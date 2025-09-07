"""
MongoDB数据库管理器，使用单集合设计。
将凭证、状态、配置、统计数据统一存储在一个集合中。
"""
import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import deque
from dataclasses import dataclass, field

import motor.motor_asyncio
from log import log


@dataclass
class WriteOperation:
    """写入操作数据结构"""
    key: str
    data: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)
    operation_type: str = "update"  # update, delete

class MongoDBManager:
    """MongoDB数据库管理器"""
    
    def __init__(self):
        self._client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
        self._db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        
        # 配置
        self._connection_uri = None
        self._database_name = None
        
        # 单文档设计 - 所有凭证存在一个文档中（类似TOML文件）
        self._collection_name = "credentials_data"
        
        # 并发控制
        self._semaphore = asyncio.Semaphore(100)
        
        # 性能监控
        self._operation_count = 0
        self._operation_times = deque(maxlen=5000)
        
        # 单文档读写缓存（类似文件模式但更简单）
        self._credentials_cache: Dict[str, Any] = {}  # 完整的凭证数据缓存
        self._cache_dirty = False  # 标记缓存是否需要写回
        self._cache_lock = asyncio.Lock()
        self._write_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._last_cache_time = 0  # 上次缓存更新时间
        
        # 文档key定义
        self._credentials_doc_key = "all_credentials"
        self._config_doc_key = "config_data"
        
        # 写入配置参数
        self._write_delay = 1.0  # 写入延迟（秒）
        self._cache_ttl = 300  # 缓存TTL（秒）
    
    async def initialize(self):
        """初始化MongoDB连接"""
        async with self._lock:
            if self._initialized:
                return
                
            try:
                # 获取连接配置
                self._connection_uri = os.getenv("MONGODB_URI")
                self._database_name = os.getenv("MONGODB_DATABASE", "gcli2api")
                
                if not self._connection_uri:
                    raise ValueError("MONGODB_URI environment variable is required")
                
                # 建立连接
                self._client = motor.motor_asyncio.AsyncIOMotorClient(
                    self._connection_uri,
                    serverSelectionTimeoutMS=5000,
                    maxPoolSize=100,
                    minPoolSize=10,
                    maxIdleTimeMS=45000,
                    waitQueueTimeoutMS=10000,
                )
                
                # 验证连接
                await self._client.admin.command('ping')
                
                # 获取数据库
                self._db = self._client[self._database_name]
                
                # 创建索引
                await self._create_indexes()
                
                # 启动缓存写回任务
                await self._start_cache_writer()
                
                self._initialized = True
                log.info(f"MongoDB connection established to {self._database_name} with batch mode")
                
            except Exception as e:
                log.error(f"Error initializing MongoDB: {e}")
                raise
    
    async def _create_indexes(self):
        """创建简单索引（单文档设计）"""
        try:
            # 单文档设计只需要主键索引
            await self._db[self._collection_name].create_index("key", unique=True)
            await self._db[self._collection_name].create_index("updated_at")
            
            log.info("MongoDB indexes created for single-document design")
            
        except Exception as e:
            log.error(f"Error creating MongoDB indexes: {e}")
    
    async def close(self):
        """关闭MongoDB连接"""
        # 停止缓存写回任务
        await self._stop_cache_writer()
        
        # 刷新缓存到数据库
        await self._flush_cache()
        
        if self._client:
            self._client.close()
            self._initialized = False
            log.info("MongoDB connection closed with cache flushed")
    
    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("MongoDB manager not initialized")
    
    def _get_default_state(self) -> Dict[str, Any]:
        """获取默认状态数据"""
        return {
            "error_codes": [],
            "disabled": False,
            "last_success": time.time(),
            "user_email": None,
        }
    
    def _get_default_stats(self) -> Dict[str, Any]:
        """获取默认统计数据"""
        return {
            "gemini_2_5_pro_calls": 0,
            "total_calls": 0,
            "next_reset_time": None,
            "daily_limit_gemini_2_5_pro": 100,
            "daily_limit_total": 1000
        }
    
    # ============ 凭证管理 ============
    
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据到缓存（延迟写回MongoDB）"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 更新缓存中的凭证数据
                if filename not in self._credentials_cache:
                    self._credentials_cache[filename] = {
                        "credential": credential_data,
                        "state": self._get_default_state(),
                        "stats": self._get_default_stats()
                    }
                else:
                    self._credentials_cache[filename]["credential"] = credential_data
                
                self._cache_dirty = True
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Stored credential to cache: {filename} in {operation_time:.3f}s")
                return True
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error storing credential {filename} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """从缓存获取凭证数据"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if filename in self._credentials_cache:
                    return self._credentials_cache[filename]["credential"]
                return None
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error retrieving credential {filename} in {operation_time:.3f}s: {e}")
                return None
    
    async def list_credentials(self) -> List[str]:
        """从缓存列出所有凭证文件名"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                filenames = list(self._credentials_cache.keys())
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Listed {len(filenames)} credentials from cache in {operation_time:.3f}s")
                return filenames
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error listing credentials in {operation_time:.3f}s: {e}")
                return []
    
    async def delete_credential(self, filename: str) -> bool:
        """从缓存删除凭证及所有相关数据"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 从缓存中删除凭证
                if filename in self._credentials_cache:
                    del self._credentials_cache[filename]
                    self._cache_dirty = True
                    
                    # 性能监控
                    self._operation_count += 1
                    operation_time = time.time() - start_time
                    self._operation_times.append(operation_time)
                    
                    log.debug(f"Deleted credential from cache: {filename} in {operation_time:.3f}s")
                    return True
                else:
                    log.warning(f"Credential not found for deletion: {filename}")
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error deleting credential {filename} in {operation_time:.3f}s: {e}")
                return False
    
    # ============ 状态管理 ============
    
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态（使用缓存模式）"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 确保凭证存在于缓存中
                if filename not in self._credentials_cache:
                    self._credentials_cache[filename] = {
                        "credential": {},
                        "state": self._get_default_state(),
                        "stats": self._get_default_stats()
                    }
                
                # 更新状态数据
                self._credentials_cache[filename]["state"].update(state_updates)
                self._cache_dirty = True
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Updated credential state in cache: {filename} in {operation_time:.3f}s")
                return True
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error updating credential state {filename} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """从缓存获取凭证状态"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if filename in self._credentials_cache:
                    log.debug(f"Retrieved credential state from cache: {filename} in {operation_time:.3f}s")
                    return self._credentials_cache[filename]["state"]
                else:
                    # 返回默认状态
                    return self._get_default_state()
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting credential state {filename} in {operation_time:.3f}s: {e}")
                return self._get_default_state()
    
    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """从缓存获取所有凭证状态"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                states = {}
                for filename, cred_data in self._credentials_cache.items():
                    states[filename] = cred_data.get("state", self._get_default_state())
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Retrieved all credential states from cache ({len(states)}) in {operation_time:.3f}s")
                return states
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting all credential states in {operation_time:.3f}s: {e}")
                return {}
    
    # ============ 配置管理 ============
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置到配置文档"""
        async with self._semaphore:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                now = datetime.now(timezone.utc)
                
                # 使用upsert更新配置文档中的特定字段
                result = await self._db[self._collection_name].update_one(
                    {"key": self._config_doc_key},
                    {
                        "$set": {
                            f"config.{key}": value,
                            "updated_at": now
                        },
                        "$setOnInsert": {
                            "key": self._config_doc_key,
                            "config": {},
                            "created_at": now
                        }
                    },
                    upsert=True
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if result.upserted_id or result.modified_count > 0:
                    log.debug(f"Set config: {key} in {operation_time:.3f}s")
                    return True
                else:
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error setting config {key} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """从配置文档获取配置"""
        async with self._semaphore:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 从配置文档获取
                doc = await self._db[self._collection_name].find_one(
                    {"key": self._config_doc_key}
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if doc and "config" in doc and key in doc["config"]:
                    log.debug(f"Retrieved config: {key} in {operation_time:.3f}s")
                    return doc["config"][key]
                else:
                    return default
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting config {key} in {operation_time:.3f}s: {e}")
                return default
    
    async def get_all_config(self) -> Dict[str, Any]:
        """从配置文档获取所有配置"""
        async with self._semaphore:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                doc = await self._db[self._collection_name].find_one(
                    {"key": self._config_doc_key}
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if doc and "config" in doc:
                    log.debug(f"Retrieved all configs ({len(doc['config'])}) in {operation_time:.3f}s")
                    return doc["config"]
                else:
                    return {}
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting all configs in {operation_time:.3f}s: {e}")
                return {}
    
    async def delete_config(self, key: str) -> bool:
        """从统一集合删除配置"""
        async with self._semaphore:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                result = await self._db[self._collection_name].update_one(
                    {"key": self._config_doc_key},
                    {"$unset": {f"config.{key}": ""}}
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if result.deleted_count > 0:
                    log.debug(f"Deleted config: {key} in {operation_time:.3f}s")
                    return True
                else:
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error deleting config {key} in {operation_time:.3f}s: {e}")
                return False
    
    # ============ 使用统计管理 ============
    
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计（使用缓存模式）"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                # 确保凭证存在于缓存中
                if filename not in self._credentials_cache:
                    self._credentials_cache[filename] = {
                        "credential": {},
                        "state": self._get_default_state(),
                        "stats": self._get_default_stats()
                    }
                
                # 更新统计数据
                self._credentials_cache[filename]["stats"].update(stats_updates)
                self._cache_dirty = True
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Updated usage stats in cache: {filename} in {operation_time:.3f}s")
                return True
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error updating usage stats {filename} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """从统一集合获取使用统计"""
        async with self._semaphore:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                if filename in self._credentials_cache:
                    doc = {"stats": self._credentials_cache[filename].get("stats", self._get_default_stats())}
                else:
                    doc = None
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if doc and "stats" in doc:
                    log.debug(f"Retrieved usage stats: {filename} in {operation_time:.3f}s")
                    return doc["stats"]
                else:
                    return self._get_default_stats()
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting usage stats {filename} in {operation_time:.3f}s: {e}")
                return self._get_default_stats()
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """从缓存获取所有使用统计"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保缓存已加载
                await self._ensure_cache_loaded()
                
                stats = {}
                for filename, cred_data in self._credentials_cache.items():
                    if "stats" in cred_data:
                        stats[filename] = cred_data["stats"]
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Retrieved all usage stats from cache ({len(stats)}) in {operation_time:.3f}s")
                return stats
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting all usage stats in {operation_time:.3f}s: {e}")
                return {}
    
    # ==================== 单文档缓存方法（类似TOML文件设计） ====================
    
    async def _start_cache_writer(self):
        """启动缓存写回任务"""
        if self._write_task and not self._write_task.done():
            return
        
        self._shutdown_event.clear()
        self._write_task = asyncio.create_task(self._cache_writer_loop())
        log.debug("MongoDB cache writer started")
    
    async def _stop_cache_writer(self):
        """停止缓存写回任务"""
        self._shutdown_event.set()
        
        if self._write_task and not self._write_task.done():
            try:
                await asyncio.wait_for(self._write_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._write_task.cancel()
                log.warning("Cache writer forcibly cancelled")
        
        log.debug("MongoDB cache writer stopped")
    
    async def _cache_writer_loop(self):
        """缓存写回循环"""
        while not self._shutdown_event.is_set():
            try:
                # 等待写入延迟或关闭信号
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=self._write_delay)
                    break  # 收到关闭信号
                except asyncio.TimeoutError:
                    pass  # 超时，检查是否需要写回
                
                # 如果缓存脏了，写回数据库
                async with self._cache_lock:
                    if self._cache_dirty:
                        await self._write_cache_to_db()
                
            except Exception as e:
                log.error(f"Error in cache writer loop: {e}")
                await asyncio.sleep(1)
    
    async def _ensure_cache_loaded(self):
        """确保缓存已从数据库加载"""
        current_time = time.time()
        
        # 检查缓存是否过期
        if (not self._credentials_cache and 
            current_time - self._last_cache_time > self._cache_ttl):
            
            await self._load_cache_from_db()
            self._last_cache_time = current_time
    
    async def _load_cache_from_db(self):
        """从数据库加载所有凭证到缓存"""
        try:
            start_time = time.time()
            
            # 从MongoDB获取凭证文档
            doc = await self._db[self._collection_name].find_one(
                {"key": self._credentials_doc_key}
            )
            
            if doc and "credentials" in doc:
                self._credentials_cache = doc["credentials"].copy()
                log.debug(f"Loaded {len(self._credentials_cache)} credentials from DB")
            else:
                # 如果文档不存在，创建空缓存
                self._credentials_cache = {}
                log.debug("Initialized empty credentials cache")
            
            operation_time = time.time() - start_time
            log.debug(f"Cache loaded in {operation_time:.3f}s")
            
        except Exception as e:
            log.error(f"Error loading cache from DB: {e}")
            self._credentials_cache = {}
    
    async def _write_cache_to_db(self):
        """将缓存写回到数据库单文档"""
        if not self._cache_dirty:
            return
        
        try:
            start_time = time.time()
            now = datetime.now(timezone.utc)
            
            # 准备凭证文档数据
            doc = {
                "key": self._credentials_doc_key,
                "credentials": self._credentials_cache,
                "updated_at": now
            }
            
            # 使用upsert替换整个文档
            result = await self._db[self._collection_name].replace_one(
                {"key": self._credentials_doc_key},
                doc,
                upsert=True
            )
            
            self._cache_dirty = False
            operation_time = time.time() - start_time
            
            log.debug(f"Cache written to DB in {operation_time:.3f}s (modified: {result.modified_count}, upserted: {bool(result.upserted_id)})")
            
        except Exception as e:
            log.error(f"Error writing cache to DB: {e}")
    
    async def _flush_cache(self):
        """立即刷新缓存到数据库"""
        async with self._cache_lock:
            if self._cache_dirty:
                await self._write_cache_to_db()
                log.debug("Cache flushed to database")