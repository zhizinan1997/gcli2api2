"""
MongoDB数据库管理器，用于替代本地TOML文件存储。
支持凭证管理、状态存储和配置管理的分布式部署。
"""
import asyncio
import os
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from collections import defaultdict, deque

import motor.motor_asyncio
from pymongo.errors import ConnectionFailure, AutoReconnect, ServerSelectionTimeoutError

# 初始化时直接使用环境变量避免循环依赖
from log import log


class MongoDBManager:
    """MongoDB数据库管理器，提供分布式存储能力"""
    
    def __init__(self):
        self._client: Optional[motor.motor_asyncio.AsyncIOMotorClient] = None
        self._db: Optional[motor.motor_asyncio.AsyncIOMotorDatabase] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        
        # 配置
        self._connection_uri = None
        self._database_name = None
        
        # 集合名称
        self._credentials_collection = "credentials"
        self._credential_states_collection = "credential_states"
        self._system_config_collection = "system_config"
        self._usage_stats_collection = "usage_stats"
        
        # 超级并发控制 - 适配网络稳定性
        self._semaphore = asyncio.Semaphore(100)  # 并发数控制，匹配连接池大小
        self._batch_queue = defaultdict(deque)    # 批处理队列
        self._batch_lock = asyncio.Lock()         # 批处理锁
        self._batch_task: Optional[asyncio.Task] = None  # 批处理任务
        self._batch_enabled = True                # 批处理开关
        
        # 性能监控
        self._operation_count = 0
        self._operation_times = deque(maxlen=5000)  # 保持最近5000次操作时间

    async def initialize(self):
        """初始化MongoDB连接"""
        async with self._lock:
            if self._initialized:
                return
                
            # 从配置获取MongoDB连接信息（初始化时直接使用环境变量，避免循环依赖）
            self._connection_uri = os.getenv("MONGODB_URI")
            self._database_name = os.getenv("MONGODB_DATABASE", "gcli2api")
            
            if not self._connection_uri:
                raise ValueError("MongoDB URI not configured. Set MONGODB_URI environment variable or mongodb_uri in config.toml")
            
            try:
                # 创建MongoDB连接 - 超级并发配置 + 网络稳定性优化
                self._client = motor.motor_asyncio.AsyncIOMotorClient(
                    self._connection_uri,
                    serverSelectionTimeoutMS=5000,   # 增加到5秒，处理网络不稳定
                    connectTimeoutMS=10000,          # 增加到10秒连接超时
                    socketTimeoutMS=60000,           # 增加到60秒socket超时，处理网络延迟
                    minPoolSize=100,                  # 最小连接数
                    maxIdleTimeMS=60000,            # 空闲超时
                    waitQueueTimeoutMS=5000,        # 队列等待时间
                    maxConnecting=100,               # 并发连接建立数
                    retryWrites=True,               # 重试写入
                    retryReads=True,                # 重试读取
                    heartbeatFrequencyMS=30000,     # 30秒心跳频率
                    appname="gcli2api_super_concurrent",  # 应用标识
                )
                
                # 测试连接
                await self._client.admin.command('ping')
                
                # 获取数据库
                self._db = self._client[self._database_name]
                
                # 创建索引
                await self._create_indexes()
                
                self._initialized = True
                log.info(f"MongoDB initialized successfully. Database: {self._database_name}")
                
                # 启动批处理任务
                if self._batch_enabled and not self._batch_task:
                    self._batch_task = asyncio.create_task(self._batch_processor())
                
            except ConnectionFailure as e:
                log.error(f"Failed to connect to MongoDB: {e}")
                raise
            except Exception as e:
                log.error(f"Error initializing MongoDB: {e}")
                raise
    
    async def _create_indexes(self):
        """创建必要的索引"""
        try:
            # 凭证集合索引
            await self._db[self._credentials_collection].create_index("filename", unique=True)
            await self._db[self._credentials_collection].create_index("created_at")
            await self._db[self._credentials_collection].create_index("updated_at")
            
            # 凭证状态集合索引
            await self._db[self._credential_states_collection].create_index("filename", unique=True)
            await self._db[self._credential_states_collection].create_index("disabled")
            await self._db[self._credential_states_collection].create_index("last_success")
            await self._db[self._credential_states_collection].create_index("updated_at")
            
            # 系统配置集合索引
            await self._db[self._system_config_collection].create_index("key", unique=True)
            await self._db[self._system_config_collection].create_index("updated_at")
            
            # 使用统计集合索引
            await self._db[self._usage_stats_collection].create_index("filename", unique=True)
            await self._db[self._usage_stats_collection].create_index("next_reset_time")
            await self._db[self._usage_stats_collection].create_index("updated_at")
            
            log.debug("MongoDB indexes created successfully")
            
        except Exception as e:
            log.error(f"Error creating MongoDB indexes: {e}")
            # 索引创建失败不应该阻止系统启动
    
    async def close(self):
        """关闭MongoDB连接"""
        # 关闭批处理任务
        if self._batch_task and not self._batch_task.done():
            self._batch_task.cancel()
            try:
                await self._batch_task
            except asyncio.CancelledError:
                pass
        
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
            self._initialized = False
            log.info("MongoDB connection closed")
    
    def _ensure_initialized(self):
        """确保MongoDB已初始化"""
        if not self._initialized:
            raise RuntimeError("MongoDB not initialized. Call initialize() first.")
    
    def _retry_on_network_error(self, max_retries: int = 3, delay: float = 0.5):
        """网络错误重试装饰器"""
        def decorator(func):
            async def wrapper(*args, **kwargs):
                last_exception = None
                for attempt in range(max_retries):
                    try:
                        return await func(*args, **kwargs)
                    except (AutoReconnect, ServerSelectionTimeoutError, ConnectionFailure) as e:
                        last_exception = e
                        if attempt < max_retries - 1:
                            wait_time = delay * (2 ** attempt)  # 指数退避
                            log.warning(f"MongoDB network error (attempt {attempt + 1}/{max_retries}): {e}, retrying in {wait_time}s")
                            await asyncio.sleep(wait_time)
                        else:
                            log.error(f"MongoDB network error after {max_retries} attempts: {e}")
                    except Exception as e:
                        # 非网络错误直接抛出
                        raise e
                
                raise last_exception
            return wrapper
        return decorator
    
    def _monitor_operation(self, operation_name: str):
        """操作性能监控装饰器"""
        def decorator(func):
            async def wrapper(*args, **kwargs):
                async with self._semaphore:  # 并发控制
                    start_time = time.time()
                    try:
                        result = await func(*args, **kwargs)
                        self._operation_count += 1
                        operation_time = time.time() - start_time
                        self._operation_times.append(operation_time)
                        
                        # 每100次操作输出一次性能统计
                        if self._operation_count % 100 == 0:
                            avg_time = sum(self._operation_times) / len(self._operation_times) if self._operation_times else 0
                            log.debug(f"MongoDB performance: {operation_name} avg={avg_time:.3f}s, ops={self._operation_count}, concurrent={500-self._semaphore._value}")
                        
                        return result
                    except Exception as e:
                        operation_time = time.time() - start_time
                        log.error(f"MongoDB operation failed: {operation_name} in {operation_time:.3f}s, error={e}")
                        raise
            return wrapper
        return decorator
    
    async def _batch_processor(self):
        """批处理器，将小的操作合并成批量操作"""
        while True:
            try:
                await asyncio.sleep(0.01)  # 10ms批处理间隔
                
                if not self._batch_queue:
                    continue
                
                async with self._batch_lock:
                    # 处理批量写入
                    for collection_name, operations in list(self._batch_queue.items()):
                        if not operations:
                            continue
                        
                        # 取出待处理的操作（最多100个）
                        batch_ops = []
                        for _ in range(min(100, len(operations))):
                            if operations:
                                batch_ops.append(operations.popleft())
                        
                        if not batch_ops:
                            continue
                        
                        # 执行批量操作
                        try:
                            if batch_ops:
                                collection = self._db[collection_name]
                                # 使用bulk_write进行批量操作
                                from pymongo import ReplaceOne
                                bulk_operations = []
                                for op in batch_ops:
                                    if op['type'] == 'replace':
                                        bulk_operations.append(
                                            ReplaceOne(op['filter'], op['replacement'], upsert=op.get('upsert', False))
                                        )
                                
                                if bulk_operations:
                                    result = await collection.bulk_write(bulk_operations, ordered=False)
                                    log.debug(f"Batch operation completed: {len(bulk_operations)} ops, modified={result.modified_count}, upserted={result.upserted_count}")
                                    
                                    # 通知完成
                                    for op in batch_ops:
                                        if 'future' in op and not op['future'].done():
                                            op['future'].set_result(True)
                        
                        except Exception as e:
                            log.error(f"Batch operation failed: {e}")
                            # 通知失败
                            for op in batch_ops:
                                if 'future' in op and not op['future'].done():
                                    op['future'].set_exception(e)
                        
                        # 如果队列为空，删除该集合的队列
                        if not operations:
                            del self._batch_queue[collection_name]
                            
            except asyncio.CancelledError:
                log.debug("Batch processor cancelled")
                break
            except Exception as e:
                log.error(f"Batch processor error: {e}")
                await asyncio.sleep(0.1)  # 出错后稍微延迟
    
    # ============ 凭证管理 ============
    
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据"""
        @self._retry_on_network_error(max_retries=3, delay=0.5)
        async def _store_with_retry():
            # 准备文档
            now = datetime.now(timezone.utc)
            doc = {
                "filename": filename,
                "credential_data": credential_data,
                "created_at": now,
                "updated_at": now,
            }
            
            # 使用upsert操作
            result = await self._db[self._credentials_collection].replace_one(
                {"filename": filename},
                doc,
                upsert=True
            )
            return result
        
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                result = await _store_with_retry()
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if result.upserted_id or result.modified_count > 0:
                    log.debug(f"Stored credential: {filename} in {operation_time:.3f}s")
                    return True
                else:
                    log.warning(f"No changes made to credential: {filename}")
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error storing credential {filename} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                doc = await self._db[self._credentials_collection].find_one(
                    {"filename": filename},
                    {"credential_data": 1, "_id": 0}
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if doc:
                    log.debug(f"Retrieved credential: {filename} in {operation_time:.3f}s")
                    return doc["credential_data"]
                return None
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error retrieving credential {filename} in {operation_time:.3f}s: {e}")
                return None
    
    async def list_credentials(self) -> List[str]:
        """列出所有凭证文件名"""
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                cursor = self._db[self._credentials_collection].find(
                    {},
                    {"filename": 1, "_id": 0}
                ).sort("created_at", 1)
                
                filenames = []
                async for doc in cursor:
                    filenames.append(doc["filename"])
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                log.debug(f"Listed {len(filenames)} credentials in {operation_time:.3f}s")
                return filenames
                
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error listing credentials in {operation_time:.3f}s: {e}")
                return []
    
    async def delete_credential(self, filename: str) -> bool:
        """删除凭证"""
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                # 并发执行所有删除操作以提升性能
                tasks = [
                    self._db[self._credentials_collection].delete_one({"filename": filename}),
                    self._db[self._credential_states_collection].delete_one({"filename": filename}),
                    self._db[self._usage_stats_collection].delete_one({"filename": filename})
                ]
                
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                # 检查主凭证删除结果
                if isinstance(results[0], Exception):
                    raise results[0]
                    
                if results[0].deleted_count > 0:
                    log.debug(f"Deleted credential and related data: {filename} in {operation_time:.3f}s")
                    return True
                else:
                    log.warning(f"Credential not found for deletion: {filename}")
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error deleting credential {filename} in {operation_time:.3f}s: {e}")
                return False
    
    # ============ 凭证状态管理 ============
    
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态"""
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                now = datetime.now(timezone.utc)
                
                # 准备更新文档
                update_doc = {
                    "$set": {
                        **state_updates,
                        "updated_at": now
                    },
                    "$setOnInsert": {
                        "filename": filename,
                        "created_at": now,
                    }
                }
                
                result = await self._db[self._credential_states_collection].update_one(
                    {"filename": filename},
                    update_doc,
                    upsert=True
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if result.upserted_id or result.modified_count > 0:
                    log.debug(f"Updated credential state: {filename} in {operation_time:.3f}s")
                    return True
                else:
                    log.debug(f"No changes to credential state: {filename}")
                    return False
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error updating credential state {filename} in {operation_time:.3f}s: {e}")
                return False
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """获取凭证状态"""
        async with self._semaphore:  # 并发控制
            self._ensure_initialized()
            start_time = time.time()
            
            try:
                doc = await self._db[self._credential_states_collection].find_one(
                    {"filename": filename},
                    {"_id": 0, "filename": 0, "created_at": 0}
                )
                
                # 性能监控
                self._operation_count += 1
                operation_time = time.time() - start_time
                self._operation_times.append(operation_time)
                
                if doc:
                    # 移除MongoDB特定字段
                    doc.pop("updated_at", None)
                    log.debug(f"Retrieved credential state: {filename} in {operation_time:.3f}s")
                    return doc
                else:
                    # 返回默认状态
                    log.debug(f"Using default credential state for: {filename} in {operation_time:.3f}s")
                    return {
                        "error_codes": [],
                        "disabled": False,
                        "last_success": time.time(),
                        "user_email": None,
                    }
                    
            except Exception as e:
                operation_time = time.time() - start_time
                log.error(f"Error getting credential state {filename} in {operation_time:.3f}s: {e}")
                return {
                    "error_codes": [],
                    "disabled": False,
                    "last_success": time.time(),
                    "user_email": None,
                }
    
    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        self._ensure_initialized()
        
        try:
            cursor = self._db[self._credential_states_collection].find(
                {},
                {"_id": 0, "created_at": 0}
            )
            
            states = {}
            async for doc in cursor:
                filename = doc.pop("filename")
                doc.pop("updated_at", None)
                states[filename] = doc
            
            return states
            
        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}
    
    # ============ 系统配置管理 ============
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项"""
        self._ensure_initialized()
        
        try:
            now = datetime.now(timezone.utc)
            doc = {
                "key": key,
                "value": value,
                "updated_at": now,
            }
            
            result = await self._db[self._system_config_collection].replace_one(
                {"key": key},
                doc,
                upsert=True
            )
            
            if result.upserted_id or result.modified_count > 0:
                log.debug(f"Set config: {key} = {value}")
                return True
            else:
                log.debug(f"No change to config: {key}")
                return False
                
        except Exception as e:
            log.error(f"Error setting config {key}: {e}")
            return False
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        self._ensure_initialized()
        
        try:
            doc = await self._db[self._system_config_collection].find_one(
                {"key": key},
                {"value": 1, "_id": 0}
            )
            
            if doc:
                return doc["value"]
            return default
            
        except Exception as e:
            log.error(f"Error getting config {key}: {e}")
            return default
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        self._ensure_initialized()
        
        try:
            cursor = self._db[self._system_config_collection].find(
                {},
                {"key": 1, "value": 1, "_id": 0}
            )
            
            config = {}
            async for doc in cursor:
                config[doc["key"]] = doc["value"]
            
            return config
            
        except Exception as e:
            log.error(f"Error getting all config: {e}")
            return {}
    
    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        self._ensure_initialized()
        
        try:
            result = await self._db[self._system_config_collection].delete_one(
                {"key": key}
            )
            
            if result.deleted_count > 0:
                log.debug(f"Deleted config: {key}")
                return True
            else:
                log.warning(f"Config not found for deletion: {key}")
                return False
                
        except Exception as e:
            log.error(f"Error deleting config {key}: {e}")
            return False
    
    # ============ 使用统计管理 ============
    
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计"""
        self._ensure_initialized()
        
        try:
            now = datetime.now(timezone.utc)
            
            update_doc = {
                "$set": {
                    **stats_updates,
                    "updated_at": now
                },
                "$setOnInsert": {
                    "filename": filename,
                    "created_at": now,
                }
            }
            
            result = await self._db[self._usage_stats_collection].update_one(
                {"filename": filename},
                update_doc,
                upsert=True
            )
            
            if result.upserted_id or result.modified_count > 0:
                log.debug(f"Updated usage stats: {filename}")
                return True
            else:
                log.debug(f"No change to usage stats: {filename}")
                return False
                
        except Exception as e:
            log.error(f"Error updating usage stats {filename}: {e}")
            return False
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """获取使用统计"""
        self._ensure_initialized()
        
        try:
            doc = await self._db[self._usage_stats_collection].find_one(
                {"filename": filename},
                {"_id": 0, "filename": 0, "created_at": 0}
            )
            
            if doc:
                doc.pop("updated_at", None)
                return doc
            else:
                # 返回默认统计
                return {
                    "gemini_2_5_pro_calls": 0,
                    "total_calls": 0,
                    "next_reset_time": None,
                    "daily_limit_gemini_2_5_pro": 100,
                    "daily_limit_total": 1000
                }
                
        except Exception as e:
            log.error(f"Error getting usage stats {filename}: {e}")
            return {
                "gemini_2_5_pro_calls": 0,
                "total_calls": 0,
                "next_reset_time": None,
                "daily_limit_gemini_2_5_pro": 100,
                "daily_limit_total": 1000
            }
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有使用统计"""
        self._ensure_initialized()
        
        try:
            cursor = self._db[self._usage_stats_collection].find(
                {},
                {"_id": 0, "created_at": 0}
            )
            
            stats = {}
            async for doc in cursor:
                filename = doc.pop("filename")
                doc.pop("updated_at", None)
                stats[filename] = doc
            
            return stats
            
        except Exception as e:
            log.error(f"Error getting all usage stats: {e}")
            return {}
    
    # ============ 工具方法 ============
    
    async def is_available(self) -> bool:
        """检查MongoDB是否可用"""
        if not self._initialized:
            return False
        
        try:
            await self._client.admin.command('ping')
            return True
        except Exception:
            return False
    
    async def get_database_info(self) -> Dict[str, Any]:
        """获取数据库信息"""
        self._ensure_initialized()
        
        try:
            # 获取数据库统计
            stats = await self._db.command("dbStats")
            
            # 获取集合统计
            collections_info = {}
            for collection_name in [self._credentials_collection, 
                                  self._credential_states_collection,
                                  self._system_config_collection,
                                  self._usage_stats_collection]:
                try:
                    count = await self._db[collection_name].count_documents({})
                    collections_info[collection_name] = {
                        "document_count": count
                    }
                except Exception as e:
                    collections_info[collection_name] = {
                        "document_count": 0,
                        "error": str(e)
                    }
            
            return {
                "database_name": self._database_name,
                "collections": collections_info,
                "db_size_bytes": stats.get("dataSize", 0),
                "storage_size_bytes": stats.get("storageSize", 0),
            }
            
        except Exception as e:
            log.error(f"Error getting database info: {e}")
            return {"error": str(e)}


# 全局实例管理
_mongodb_manager: Optional[MongoDBManager] = None


async def get_mongodb_manager() -> MongoDBManager:
    """获取MongoDB管理器的全局实例"""
    global _mongodb_manager
    
    if _mongodb_manager is None:
        _mongodb_manager = MongoDBManager()
        await _mongodb_manager.initialize()
    
    return _mongodb_manager


async def close_mongodb_manager():
    """关闭MongoDB管理器"""
    global _mongodb_manager
    
    if _mongodb_manager:
        await _mongodb_manager.close()
        _mongodb_manager = None