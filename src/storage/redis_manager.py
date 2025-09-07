"""
Redis数据库管理器，使用哈希表设计。
所有凭证数据存储在一个哈希表中，配置数据存储在另一个哈希表中。
"""
import asyncio
import json
import os
import time
from typing import Dict, Any, List, Optional
from collections import deque

import redis.asyncio as redis
from log import log


class RedisManager:
    """Redis数据库管理器"""
    
    def __init__(self):
        self._client: Optional[redis.Redis] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        
        # 配置
        self._connection_uri = None
        self._database_index = 0
        
        # 哈希表设计 - 所有凭证存在一个哈希表中
        self._credentials_hash_name = "gcli2api:credentials"
        self._config_hash_name = "gcli2api:config"
        
        # 性能监控
        self._operation_count = 0
        self._operation_times = deque(maxlen=5000)
        
        # 哈希表缓存（类似文件模式但更简单）
        self._credentials_cache: Dict[str, Any] = {}  # 完整的凭证数据缓存
        self._config_cache: Dict[str, Any] = {}  # 配置数据缓存
        self._cache_dirty = False  # 标记缓存是否需要写回
        self._config_dirty = False  # 标记配置缓存是否需要写回
        self._cache_lock = asyncio.Lock()
        self._write_task: Optional[asyncio.Task] = None
        self._shutdown_event = asyncio.Event()
        self._last_cache_time = 0  # 上次缓存更新时间
        self._last_config_cache_time = 0  # 上次配置缓存更新时间
        
        # 写入配置参数
        self._write_delay = 1.0  # 写入延迟（秒）
        self._cache_ttl = 300  # 缓存TTL（秒）
    
    async def initialize(self):
        """初始化Redis连接"""
        async with self._lock:
            if self._initialized:
                return
                
            try:
                # 获取连接配置
                self._connection_uri = os.getenv("REDIS_URI", "redis://localhost:6379")
                self._database_index = int(os.getenv("REDIS_DATABASE", "0"))
                
                # 建立连接 - 使用最简配置确保兼容性
                # 检查是否需要 SSL
                if self._connection_uri.startswith("rediss://"):
                    # SSL 连接
                    self._client = redis.from_url(
                        self._connection_uri,
                        db=self._database_index,
                        decode_responses=True,
                        ssl_cert_reqs=None,
                        ssl_check_hostname=False,
                        ssl_ca_certs=None
                    )
                else:
                    # 普通连接
                    self._client = redis.from_url(
                        self._connection_uri,
                        db=self._database_index,
                        decode_responses=True
                    )
                
                # 验证连接
                await self._client.ping()
                
                # 启动缓存写回任务
                await self._start_cache_writer()
                
                self._initialized = True
                log.info(f"Redis connection established to database {self._database_index} with batch mode")
                
            except Exception as e:
                log.error(f"Error initializing Redis: {e}")
                raise
    
    async def close(self):
        """关闭Redis连接"""
        # 停止缓存写回任务
        await self._stop_cache_writer()
        
        # 刷新缓存到数据库
        await self._flush_cache()
        
        if self._client:
            await self._client.close()
            self._initialized = False
            log.info("Redis connection closed with cache flushed")
    
    def _ensure_initialized(self):
        """确保已初始化"""
        if not self._initialized:
            raise RuntimeError("Redis manager not initialized")
    
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
        """存储凭证数据到缓存（延迟写回Redis）"""
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
        """设置配置到缓存（延迟写回Redis）"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保配置缓存已加载
                await self._ensure_config_cache_loaded()
                
                # 更新配置缓存
                self._config_cache[key] = value
                self._config_dirty = True
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Set config to cache: {key} in {operation_time:.3f}s")
                return True
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error setting config {key} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """从配置缓存获取配置"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保配置缓存已加载
                await self._ensure_config_cache_loaded()
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if key in self._config_cache:
                    log.debug(f"Retrieved config from cache: {key} in {operation_time:.3f}s")
                    return self._config_cache[key]
                else:
                    return default
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting config {key} in {operation_time:.3f}s: {e}")
                return default
    
    async def get_all_config(self) -> Dict[str, Any]:
        """从配置缓存获取所有配置"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保配置缓存已加载
                await self._ensure_config_cache_loaded()
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Retrieved all configs from cache ({len(self._config_cache)}) in {operation_time:.3f}s")
                return self._config_cache.copy()
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting all configs in {operation_time:.3f}s: {e}")
                return {}
    
    async def delete_config(self, key: str) -> bool:
        """从配置缓存删除配置"""
        async with self._cache_lock:
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 确保配置缓存已加载
                await self._ensure_config_cache_loaded()
                
                if key in self._config_cache:
                    del self._config_cache[key]
                    self._config_dirty = True
                    
                    # 性能监控
                    self._operation_count += 1
                    operation_time = time.time() - start_time
                    self._operation_times.append(operation_time)
                    
                    log.debug(f"Deleted config from cache: {key} in {operation_time:.3f}s")
                    return True
                else:
                    log.warning(f"Config not found for deletion: {key}")
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
        """从缓存获取使用统计"""
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
                    log.debug(f"Retrieved usage stats from cache: {filename} in {operation_time:.3f}s")
                    return self._credentials_cache[filename].get("stats", self._get_default_stats())
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
    
    # ==================== 哈希表缓存方法 ====================
    
    async def _start_cache_writer(self):
        """启动缓存写回任务"""
        if self._write_task and not self._write_task.done():
            return
        
        self._shutdown_event.clear()
        self._write_task = asyncio.create_task(self._cache_writer_loop())
        log.debug("Redis cache writer started")
    
    async def _stop_cache_writer(self):
        """停止缓存写回任务"""
        self._shutdown_event.set()
        
        if self._write_task and not self._write_task.done():
            try:
                await asyncio.wait_for(self._write_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._write_task.cancel()
                log.warning("Cache writer forcibly cancelled")
        
        log.debug("Redis cache writer stopped")
    
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
                    
                    if self._config_dirty:
                        await self._write_config_cache_to_db()
                
            except Exception as e:
                log.error(f"Error in cache writer loop: {e}")
                await asyncio.sleep(1)
    
    async def _ensure_cache_loaded(self):
        """确保缓存已从数据库加载"""
        current_time = time.time()
        
        # 检查缓存是否需要加载（首次加载或过期）
        if (self._last_cache_time == 0 or 
            current_time - self._last_cache_time > self._cache_ttl):
            
            await self._load_cache_from_db()
            self._last_cache_time = current_time
    
    async def _load_cache_from_db(self):
        """从Redis加载所有凭证到缓存"""
        try:
            start_time = time.time()
            
            # 从Redis获取哈希表所有数据
            hash_data = await self._client.hgetall(self._credentials_hash_name)
            
            if hash_data:
                # 反序列化JSON数据
                self._credentials_cache = {}
                for filename, data_str in hash_data.items():
                    try:
                        self._credentials_cache[filename] = json.loads(data_str)
                    except json.JSONDecodeError as e:
                        log.error(f"Error deserializing credential {filename}: {e}")
                
                log.debug(f"Loaded {len(self._credentials_cache)} credentials from Redis")
            else:
                # 如果哈希表不存在，创建空缓存
                self._credentials_cache = {}
                log.debug("Initialized empty credentials cache")
            
            operation_time = time.time() - start_time
            log.debug(f"Cache loaded in {operation_time:.3f}s")
            
        except Exception as e:
            log.error(f"Error loading cache from Redis: {e}")
            self._credentials_cache = {}
    
    async def _write_cache_to_db(self):
        """将缓存写回到Redis哈希表"""
        if not self._cache_dirty:
            return
        
        try:
            start_time = time.time()
            
            # 准备序列化数据
            hash_data = {}
            for filename, cred_data in self._credentials_cache.items():
                try:
                    hash_data[filename] = json.dumps(cred_data, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    log.error(f"Error serializing credential {filename}: {e}")
                    continue
            
            # 使用管道批量写入
            pipe = self._client.pipeline()
            
            # 删除旧的哈希表（如果存在）
            pipe.delete(self._credentials_hash_name)
            
            # 批量设置新数据
            if hash_data:
                pipe.hset(self._credentials_hash_name, mapping=hash_data)
            
            # 执行管道
            await pipe.execute()
            
            self._cache_dirty = False
            operation_time = time.time() - start_time
            
            log.debug(f"Cache written to Redis in {operation_time:.3f}s ({len(hash_data)} credentials)")
            
        except Exception as e:
            log.error(f"Error writing cache to Redis: {e}")
    
    async def _flush_cache(self):
        """立即刷新缓存到数据库"""
        async with self._cache_lock:
            if self._cache_dirty:
                await self._write_cache_to_db()
                log.debug("Cache flushed to Redis")
            
            if self._config_dirty:
                await self._write_config_cache_to_db()
                log.debug("Config cache flushed to Redis")
    
    # ==================== 配置缓存方法 ====================
    
    async def _ensure_config_cache_loaded(self):
        """确保配置缓存已从数据库加载"""
        current_time = time.time()
        
        # 检查缓存是否需要加载（首次加载或过期）
        if (self._last_config_cache_time == 0 or 
            current_time - self._last_config_cache_time > self._cache_ttl):
            
            await self._load_config_cache_from_db()
            self._last_config_cache_time = current_time
    
    async def _load_config_cache_from_db(self):
        """从Redis加载配置到缓存"""
        try:
            start_time = time.time()
            
            # 从Redis获取配置哈希表
            hash_data = await self._client.hgetall(self._config_hash_name)
            
            if hash_data:
                # 反序列化JSON数据
                self._config_cache = {}
                for key, value_str in hash_data.items():
                    try:
                        self._config_cache[key] = json.loads(value_str)
                    except json.JSONDecodeError as e:
                        log.error(f"Error deserializing config {key}: {e}")
                        # 对于简单字符串值，直接使用
                        self._config_cache[key] = value_str
                
                log.debug(f"Loaded {len(self._config_cache)} configs from Redis")
            else:
                # 如果哈希表不存在，创建空缓存
                self._config_cache = {}
                log.debug("Initialized empty config cache")
            
            operation_time = time.time() - start_time
            log.debug(f"Config cache loaded in {operation_time:.3f}s")
            
        except Exception as e:
            log.error(f"Error loading config cache from Redis: {e}")
            self._config_cache = {}
    
    async def _write_config_cache_to_db(self):
        """将配置缓存写回到Redis"""
        if not self._config_dirty:
            return
        
        try:
            start_time = time.time()
            
            # 准备序列化数据
            hash_data = {}
            for key, value in self._config_cache.items():
                try:
                    if isinstance(value, (dict, list)):
                        hash_data[key] = json.dumps(value, ensure_ascii=False)
                    else:
                        # 对于简单类型，转换为字符串
                        hash_data[key] = json.dumps(value, ensure_ascii=False)
                except (TypeError, ValueError) as e:
                    log.error(f"Error serializing config {key}: {e}")
                    continue
            
            # 使用管道批量写入
            pipe = self._client.pipeline()
            
            # 删除旧的哈希表（如果存在）
            pipe.delete(self._config_hash_name)
            
            # 批量设置新数据
            if hash_data:
                pipe.hset(self._config_hash_name, mapping=hash_data)
            
            # 执行管道
            await pipe.execute()
            
            self._config_dirty = False
            operation_time = time.time() - start_time
            
            log.debug(f"Config cache written to Redis in {operation_time:.3f}s ({len(hash_data)} configs)")
            
        except Exception as e:
            log.error(f"Error writing config cache to Redis: {e}")