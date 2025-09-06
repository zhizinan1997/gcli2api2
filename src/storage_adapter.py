"""
存储适配器，提供统一的接口来处理本地文件存储和MongoDB存储。
根据配置自动选择存储后端。
"""
import asyncio
import os
import json
import time
import hashlib
from typing import Dict, Any, List, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    pass
from dataclasses import dataclass, field
from collections import OrderedDict

import aiofiles
import toml

from log import log


@dataclass
class CacheEntry:
    """缓存条目"""
    value: Any
    timestamp: float = field(default_factory=time.time)
    access_count: int = 0
    
    def is_expired(self, ttl: float) -> bool:
        """检查是否过期"""
        return time.time() - self.timestamp > ttl
    
    def touch(self):
        """更新访问时间和计数"""
        self.timestamp = time.time()
        self.access_count += 1


class HighPerformanceCache:
    """高性能内存缓存
    
    特性:
    - LRU淘汰策略
    - TTL过期机制
    - 读写穿透缓存
    - 异步锁保证线程安全
    - 内存使用监控
    """
    
    def __init__(self, max_size: int = 1000, default_ttl: float = 300.0):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self._stats = {
            'hits': 0,
            'misses': 0,
            'evictions': 0,
            'sets': 0
        }
    
    def _make_key(self, operation: str, *args) -> str:
        """生成缓存键"""
        key_data = f"{operation}:{':'.join(str(arg) for arg in args)}"
        return hashlib.md5(key_data.encode()).hexdigest()[:16]
    
    async def _get_lock(self, key: str) -> asyncio.Lock:
        """获取键专用锁"""
        async with self._global_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]
    
    async def get(self, operation: str, *args) -> Optional[Any]:
        """获取缓存值"""
        key = self._make_key(operation, *args)
        lock = await self._get_lock(key)
        
        async with lock:
            if key in self._cache:
                entry = self._cache[key]
                
                # 检查是否过期
                if entry.is_expired(self.default_ttl):
                    del self._cache[key]
                    self._stats['misses'] += 1
                    return None
                
                # 更新访问信息并移动到末尾（LRU）
                entry.touch()
                self._cache.move_to_end(key)
                self._stats['hits'] += 1
                return entry.value
            
            self._stats['misses'] += 1
            return None
    
    async def set(self, operation: str, value: Any, *args, ttl: Optional[float] = None) -> None:
        """设置缓存值"""
        key = self._make_key(operation, *args)
        lock = await self._get_lock(key)
        
        async with lock:
            # 如果缓存已满，删除最老的条目
            while len(self._cache) >= self.max_size:
                oldest_key, _ = self._cache.popitem(last=False)
                self._stats['evictions'] += 1
                # 清理对应的锁
                if oldest_key in self._locks:
                    del self._locks[oldest_key]
            
            entry = CacheEntry(value=value)
            self._cache[key] = entry
            self._cache.move_to_end(key)
            self._stats['sets'] += 1
    
    async def delete(self, operation: str, *args) -> bool:
        """删除缓存条目"""
        key = self._make_key(operation, *args)
        lock = await self._get_lock(key)
        
        async with lock:
            if key in self._cache:
                del self._cache[key]
                if key in self._locks:
                    del self._locks[key]
                return True
            return False
    
    async def clear(self) -> None:
        """清空缓存"""
        async with self._global_lock:
            self._cache.clear()
            self._locks.clear()
            self._stats = {'hits': 0, 'misses': 0, 'evictions': 0, 'sets': 0}
    
    def get_stats(self) -> Dict[str, Any]:
        """获取缓存统计"""
        total_requests = self._stats['hits'] + self._stats['misses']
        hit_rate = self._stats['hits'] / total_requests if total_requests > 0 else 0
        
        return {
            'size': len(self._cache),
            'max_size': self.max_size,
            'hits': self._stats['hits'],
            'misses': self._stats['misses'],
            'evictions': self._stats['evictions'],
            'sets': self._stats['sets'],
            'hit_rate': hit_rate,
            'memory_usage': len(self._cache) / self.max_size
        }


class CachedStorageBackend:
    """带缓存的存储后端装饰器"""
    
    def __init__(self, backend: "StorageBackend", cache_size: int = 1000, cache_ttl: float = 300.0):
        self._backend = backend
        self._cache = HighPerformanceCache(max_size=cache_size, default_ttl=cache_ttl)
        self._write_through = True  # 写穿透模式
    
    async def initialize(self) -> None:
        """初始化存储后端"""
        await self._backend.initialize()
    
    async def close(self) -> None:
        """关闭存储后端"""
        await self._cache.clear()
        await self._backend.close()
    
    # ============ 凭证管理（带缓存） ============
    
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据（写穿透）"""
        result = await self._backend.store_credential(filename, credential_data)
        if result:
            # 写穿透：更新缓存
            await self._cache.set('get_credential', credential_data, filename)
        return result
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """获取凭证数据（读穿透）"""
        # 先查缓存
        cached_result = await self._cache.get('get_credential', filename)
        if cached_result is not None:
            return cached_result
        
        # 缓存未命中，从后端获取
        result = await self._backend.get_credential(filename)
        if result is not None:
            await self._cache.set('get_credential', result, filename)
        
        return result
    
    async def list_credentials(self) -> List[str]:
        """列出所有凭证文件名（带缓存）"""
        cached_result = await self._cache.get('list_credentials')
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.list_credentials()
        await self._cache.set('list_credentials', result)
        return result
    
    async def delete_credential(self, filename: str) -> bool:
        """删除凭证（删除缓存）"""
        result = await self._backend.delete_credential(filename)
        if result:
            # 删除相关缓存
            await self._cache.delete('get_credential', filename)
            await self._cache.delete('list_credentials')
        return result
    
    # ============ 状态管理（带缓存） ============
    
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态（写穿透）"""
        result = await self._backend.update_credential_state(filename, state_updates)
        if result:
            # 写穿透：删除旧缓存，让下次读取时重新加载
            await self._cache.delete('get_credential_state', filename)
            await self._cache.delete('get_all_credential_states')
        return result
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """获取凭证状态（读穿透）"""
        cached_result = await self._cache.get('get_credential_state', filename)
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_credential_state(filename)
        await self._cache.set('get_credential_state', result, filename)
        return result
    
    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态（带缓存）"""
        cached_result = await self._cache.get('get_all_credential_states')
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_all_credential_states()
        await self._cache.set('get_all_credential_states', result)
        return result
    
    # ============ 配置管理（带缓存） ============
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项（写穿透）"""
        result = await self._backend.set_config(key, value)
        if result:
            await self._cache.set('get_config', value, key)
            await self._cache.delete('get_all_config')
        return result
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项（读穿透）"""
        cached_result = await self._cache.get('get_config', key)
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_config(key, default)
        if result != default:
            await self._cache.set('get_config', result, key)
        return result
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置（带缓存）"""
        cached_result = await self._cache.get('get_all_config')
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_all_config()
        await self._cache.set('get_all_config', result)
        return result
    
    async def delete_config(self, key: str) -> bool:
        """删除配置项（删除缓存）"""
        result = await self._backend.delete_config(key)
        if result:
            await self._cache.delete('get_config', key)
            await self._cache.delete('get_all_config')
        return result
    
    # ============ 使用统计管理（带缓存） ============
    
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计（写穿透）"""
        result = await self._backend.update_usage_stats(filename, stats_updates)
        if result:
            await self._cache.delete('get_usage_stats', filename)
            await self._cache.delete('get_all_usage_stats')
        return result
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """获取使用统计（读穿透）"""
        cached_result = await self._cache.get('get_usage_stats', filename)
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_usage_stats(filename)
        await self._cache.set('get_usage_stats', result, filename)
        return result
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有使用统计（带缓存）"""
        cached_result = await self._cache.get('get_all_usage_stats')
        if cached_result is not None:
            return cached_result
        
        result = await self._backend.get_all_usage_stats()
        await self._cache.set('get_all_usage_stats', result)
        return result
    
    # ============ 缓存管理 ============
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        return self._cache.get_stats()
    
    async def clear_cache(self) -> None:
        """清空缓存"""
        await self._cache.clear()
        log.info("Cache cleared")
    
    async def warm_cache(self, operations: Optional[List[str]] = None) -> None:
        """预热缓存"""
        if not operations:
            operations = ['list_credentials', 'get_all_config', 'get_all_credential_states']
        
        for operation in operations:
            try:
                if operation == 'list_credentials':
                    await self.list_credentials()
                elif operation == 'get_all_config':
                    await self.get_all_config()
                elif operation == 'get_all_credential_states':
                    await self.get_all_credential_states()
                elif operation == 'get_all_usage_stats':
                    await self.get_all_usage_stats()
            except Exception as e:
                log.warning(f"Failed to warm cache for operation {operation}: {e}")
        
        log.info(f"Cache warmed for operations: {operations}")


class StorageBackend(Protocol):
    """存储后端协议"""
    
    async def initialize(self) -> None:
        """初始化存储后端"""
        ...
    
    async def close(self) -> None:
        """关闭存储后端"""
        ...
    
    # 凭证管理
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据"""
        ...
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        ...
    
    async def list_credentials(self) -> List[str]:
        """列出所有凭证文件名"""
        ...
    
    async def delete_credential(self, filename: str) -> bool:
        """删除凭证"""
        ...
    
    # 状态管理
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态"""
        ...
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """获取凭证状态"""
        ...
    
    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        ...
    
    # 配置管理
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项"""
        ...
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        ...
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        ...
    
    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        ...
    
    # 使用统计管理
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计"""
        ...
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """获取使用统计"""
        ...
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有使用统计"""
        ...


class FileStorageBackend:
    """基于本地文件的存储后端"""
    
    def __init__(self):
        self._credentials_dir = None  # 将通过异步初始化设置
        self._state_file = None
        self._config_file = None
        self._lock = asyncio.Lock()
        self._initialized = False
    
    async def initialize(self) -> None:
        """初始化文件存储"""
        if self._initialized:
            return
        
        # 获取凭证目录配置（初始化时直接使用环境变量，避免循环依赖）
        self._credentials_dir = os.getenv("CREDENTIALS_DIR", "./creds")
        self._state_file = os.path.join(self._credentials_dir, "creds_state.toml")
        self._config_file = os.path.join(self._credentials_dir, "config.toml")
        
        # 确保目录存在
        os.makedirs(self._credentials_dir, exist_ok=True)
        self._initialized = True
        log.debug("File storage backend initialized")
    
    async def close(self) -> None:
        """关闭文件存储"""
        self._initialized = False
        log.debug("File storage backend closed")
    
    def _normalize_filename(self, filename: str) -> str:
        """标准化文件名"""
        return os.path.basename(filename)
    
    def _get_credential_path(self, filename: str) -> str:
        """获取凭证文件完整路径"""
        filename = self._normalize_filename(filename)
        return os.path.join(self._credentials_dir, filename)
    
    async def _load_toml_file(self, file_path: str) -> Dict[str, Any]:
        """加载TOML文件"""
        try:
            if os.path.exists(file_path):
                async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                    content = await f.read()
                return toml.loads(content)
            return {}
        except Exception as e:
            log.error(f"Error loading TOML file {file_path}: {e}")
            return {}
    
    async def _save_toml_file(self, file_path: str, data: Dict[str, Any]) -> bool:
        """保存TOML文件"""
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            async with aiofiles.open(file_path, "w", encoding="utf-8") as f:
                await f.write(toml.dumps(data))
            return True
        except Exception as e:
            log.error(f"Error saving TOML file {file_path}: {e}")
            return False
    
    # ============ 凭证管理 ============
    
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据到JSON文件"""
        self._ensure_initialized()
        
        try:
            credential_path = self._get_credential_path(filename)
            async with aiofiles.open(credential_path, "w", encoding="utf-8") as f:
                await f.write(json.dumps(credential_data, indent=2, ensure_ascii=False))
            
            log.debug(f"Stored credential: {filename}")
            return True
            
        except Exception as e:
            log.error(f"Error storing credential {filename}: {e}")
            return False
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """从JSON文件获取凭证数据"""
        self._ensure_initialized()
        
        try:
            credential_path = self._get_credential_path(filename)
            if not os.path.exists(credential_path):
                return None
            
            async with aiofiles.open(credential_path, "r", encoding="utf-8") as f:
                content = await f.read()
            
            return json.loads(content)
            
        except Exception as e:
            log.error(f"Error getting credential {filename}: {e}")
            return None
    
    async def list_credentials(self) -> List[str]:
        """列出所有凭证文件名"""
        self._ensure_initialized()
        
        try:
            if not os.path.exists(self._credentials_dir):
                return []
            
            credentials = []
            for filename in os.listdir(self._credentials_dir):
                if filename.endswith(".json"):
                    credentials.append(filename)
            
            return sorted(credentials)
            
        except Exception as e:
            log.error(f"Error listing credentials: {e}")
            return []
    
    async def delete_credential(self, filename: str) -> bool:
        """删除凭证文件"""
        self._ensure_initialized()
        
        try:
            credential_path = self._get_credential_path(filename)
            if os.path.exists(credential_path):
                os.remove(credential_path)
                log.debug(f"Deleted credential: {filename}")
                return True
            else:
                log.warning(f"Credential file not found: {filename}")
                return False
                
        except Exception as e:
            log.error(f"Error deleting credential {filename}: {e}")
            return False
    
    # ============ 状态管理 ============
    
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态到TOML文件"""
        self._ensure_initialized()
        
        async with self._lock:
            try:
                filename = self._normalize_filename(filename)
                state_data = await self._load_toml_file(self._state_file)
                
                if filename not in state_data:
                    state_data[filename] = {}
                
                state_data[filename].update(state_updates)
                
                return await self._save_toml_file(self._state_file, state_data)
                
            except Exception as e:
                log.error(f"Error updating credential state {filename}: {e}")
                return False
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """从TOML文件获取凭证状态"""
        self._ensure_initialized()
        
        try:
            filename = self._normalize_filename(filename)
            state_data = await self._load_toml_file(self._state_file)
            
            return state_data.get(filename, {
                "error_codes": [],
                "disabled": False,
                "last_success": time.time(),
                "user_email": None,
            })
            
        except Exception as e:
            log.error(f"Error getting credential state {filename}: {e}")
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
            return await self._load_toml_file(self._state_file)
        except Exception as e:
            log.error(f"Error getting all credential states: {e}")
            return {}
    
    # ============ 配置管理 ============
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项到TOML文件"""
        self._ensure_initialized()
        
        async with self._lock:
            try:
                config_data = await self._load_toml_file(self._config_file)
                config_data[key] = value
                return await self._save_toml_file(self._config_file, config_data)
                
            except Exception as e:
                log.error(f"Error setting config {key}: {e}")
                return False
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """从TOML文件获取配置项"""
        self._ensure_initialized()
        
        try:
            config_data = await self._load_toml_file(self._config_file)
            return config_data.get(key, default)
        except Exception as e:
            log.error(f"Error getting config {key}: {e}")
            return default
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        self._ensure_initialized()
        
        try:
            return await self._load_toml_file(self._config_file)
        except Exception as e:
            log.error(f"Error getting all config: {e}")
            return {}
    
    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        self._ensure_initialized()
        
        async with self._lock:
            try:
                config_data = await self._load_toml_file(self._config_file)
                
                if key in config_data:
                    del config_data[key]
                    return await self._save_toml_file(self._config_file, config_data)
                
                return True  # 键不存在视为成功
                
            except Exception as e:
                log.error(f"Error deleting config {key}: {e}")
                return False
    
    # ============ 使用统计管理 ============
    
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计（集成到状态文件中）"""
        return await self.update_credential_state(filename, stats_updates)
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """获取使用统计"""
        state = await self.get_credential_state(filename)
        
        # 提取统计相关字段
        return {
            "gemini_2_5_pro_calls": state.get("gemini_2_5_pro_calls", 0),
            "total_calls": state.get("total_calls", 0),
            "next_reset_time": state.get("next_reset_time"),
            "daily_limit_gemini_2_5_pro": state.get("daily_limit_gemini_2_5_pro", 100),
            "daily_limit_total": state.get("daily_limit_total", 1000)
        }
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有使用统计"""
        all_states = await self.get_all_credential_states()
        
        stats = {}
        for filename, state in all_states.items():
            stats[filename] = {
                "gemini_2_5_pro_calls": state.get("gemini_2_5_pro_calls", 0),
                "total_calls": state.get("total_calls", 0),
                "next_reset_time": state.get("next_reset_time"),
                "daily_limit_gemini_2_5_pro": state.get("daily_limit_gemini_2_5_pro", 100),
                "daily_limit_total": state.get("daily_limit_total", 1000)
            }
        
        return stats
    
    def _ensure_initialized(self):
        """确保文件存储已初始化"""
        if not self._initialized:
            raise RuntimeError("File storage backend not initialized")


class StorageAdapter:
    """存储适配器，根据配置选择存储后端"""
    
    def __init__(self, enable_cache: bool = True, cache_size: int = 1000, cache_ttl: float = 300.0):
        self._backend: Optional["StorageBackend"] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        self._enable_cache = enable_cache
        self._cache_size = cache_size
        self._cache_ttl = cache_ttl
    
    async def initialize(self) -> None:
        """初始化存储适配器"""
        async with self._lock:
            if self._initialized:
                return
            
            # 检查是否配置了MongoDB（初始化时直接使用环境变量，避免循环依赖）
            mongodb_uri = os.getenv("MONGODB_URI", "")
            base_backend = None
            
            if mongodb_uri:
                # 使用MongoDB存储
                try:
                    from .mongodb_manager import MongoDBManager
                    base_backend = MongoDBManager()
                    await base_backend.initialize()
                    log.info("Using MongoDB storage backend")
                except ImportError as e:
                    log.error(f"Failed to import MongoDB backend: {e}")
                    log.info("Falling back to file storage backend")
                    base_backend = FileStorageBackend()
                    await base_backend.initialize()
                except Exception as e:
                    log.error(f"Failed to initialize MongoDB backend: {e}")
                    log.info("Falling back to file storage backend")
                    base_backend = FileStorageBackend()
                    await base_backend.initialize()
            else:
                # 使用文件存储
                base_backend = FileStorageBackend()
                await base_backend.initialize()
                log.info("Using file storage backend")
            
            # 是否启用缓存层
            if self._enable_cache:
                self._backend = CachedStorageBackend(
                    backend=base_backend,
                    cache_size=self._cache_size,
                    cache_ttl=self._cache_ttl
                )
                await self._backend.initialize()
                log.info(f"Cache layer enabled (size={self._cache_size}, ttl={self._cache_ttl}s)")
            else:
                self._backend = base_backend
                log.info("Cache layer disabled")
            
            self._initialized = True
    
    async def close(self) -> None:
        """关闭存储适配器"""
        if self._backend:
            await self._backend.close()
            self._backend = None
            self._initialized = False
    
    def _ensure_initialized(self):
        """确保存储适配器已初始化"""
        if not self._initialized or not self._backend:
            raise RuntimeError("Storage adapter not initialized")
    
    # ============ 凭证管理 ============
    
    async def store_credential(self, filename: str, credential_data: Dict[str, Any]) -> bool:
        """存储凭证数据"""
        self._ensure_initialized()
        return await self._backend.store_credential(filename, credential_data)
    
    async def get_credential(self, filename: str) -> Optional[Dict[str, Any]]:
        """获取凭证数据"""
        self._ensure_initialized()
        return await self._backend.get_credential(filename)
    
    async def list_credentials(self) -> List[str]:
        """列出所有凭证文件名"""
        self._ensure_initialized()
        return await self._backend.list_credentials()
    
    async def delete_credential(self, filename: str) -> bool:
        """删除凭证"""
        self._ensure_initialized()
        return await self._backend.delete_credential(filename)
    
    # ============ 状态管理 ============
    
    async def update_credential_state(self, filename: str, state_updates: Dict[str, Any]) -> bool:
        """更新凭证状态"""
        self._ensure_initialized()
        return await self._backend.update_credential_state(filename, state_updates)
    
    async def get_credential_state(self, filename: str) -> Dict[str, Any]:
        """获取凭证状态"""
        self._ensure_initialized()
        return await self._backend.get_credential_state(filename)
    
    async def get_all_credential_states(self) -> Dict[str, Dict[str, Any]]:
        """获取所有凭证状态"""
        self._ensure_initialized()
        return await self._backend.get_all_credential_states()
    
    # ============ 配置管理 ============
    
    async def set_config(self, key: str, value: Any) -> bool:
        """设置配置项"""
        self._ensure_initialized()
        return await self._backend.set_config(key, value)
    
    async def get_config(self, key: str, default: Any = None) -> Any:
        """获取配置项"""
        self._ensure_initialized()
        return await self._backend.get_config(key, default)
    
    async def get_all_config(self) -> Dict[str, Any]:
        """获取所有配置"""
        self._ensure_initialized()
        return await self._backend.get_all_config()
    
    async def delete_config(self, key: str) -> bool:
        """删除配置项"""
        self._ensure_initialized()
        return await self._backend.delete_config(key)
    
    # ============ 使用统计管理 ============
    
    async def update_usage_stats(self, filename: str, stats_updates: Dict[str, Any]) -> bool:
        """更新使用统计"""
        self._ensure_initialized()
        return await self._backend.update_usage_stats(filename, stats_updates)
    
    async def get_usage_stats(self, filename: str) -> Dict[str, Any]:
        """获取使用统计"""
        self._ensure_initialized()
        return await self._backend.get_usage_stats(filename)
    
    async def get_all_usage_stats(self) -> Dict[str, Dict[str, Any]]:
        """获取所有使用统计"""
        self._ensure_initialized()
        return await self._backend.get_all_usage_stats()
    
    # ============ 工具方法 ============
    
    def get_backend_type(self) -> str:
        """获取当前存储后端类型"""
        if not self._backend:
            return "none"
        
        if isinstance(self._backend, FileStorageBackend):
            return "file"
        else:
            return "mongodb"
    
    async def get_backend_info(self) -> Dict[str, Any]:
        """获取存储后端信息"""
        self._ensure_initialized()
        
        backend_type = self.get_backend_type()
        info = {
            "backend_type": backend_type,
            "initialized": self._initialized,
            "cache_enabled": self._enable_cache
        }
        
        # 获取缓存统计
        if self._enable_cache and hasattr(self._backend, 'get_cache_stats'):
            try:
                cache_stats = self._backend.get_cache_stats()
                info["cache_stats"] = cache_stats
            except Exception as e:
                info["cache_error"] = str(e)
        
        # 获取底层存储信息
        actual_backend = getattr(self._backend, '_backend', self._backend)
        if hasattr(actual_backend, 'get_database_info'):
            try:
                db_info = await actual_backend.get_database_info()
                info.update(db_info)
            except Exception as e:
                info["database_error"] = str(e)
        elif isinstance(actual_backend, FileStorageBackend):
            info.update({
                "credentials_dir": getattr(actual_backend, '_credentials_dir', None),
                "state_file": getattr(actual_backend, '_state_file', None),
                "config_file": getattr(actual_backend, '_config_file', None)
            })
        
        return info
    
    # ============ 缓存管理方法 ============
    
    def get_cache_stats(self) -> Dict[str, Any]:
        """获取缓存统计信息"""
        if not self._enable_cache or not hasattr(self._backend, 'get_cache_stats'):
            return {"cache_enabled": False}
        
        try:
            stats = self._backend.get_cache_stats()
            stats["cache_enabled"] = True
            return stats
        except Exception as e:
            return {"cache_enabled": True, "error": str(e)}
    
    async def clear_cache(self) -> bool:
        """清空缓存"""
        if not self._enable_cache or not hasattr(self._backend, 'clear_cache'):
            return False
        
        try:
            await self._backend.clear_cache()
            return True
        except Exception as e:
            log.error(f"Failed to clear cache: {e}")
            return False
    
    async def warm_cache(self, operations: Optional[List[str]] = None) -> bool:
        """预热缓存"""
        if not self._enable_cache or not hasattr(self._backend, 'warm_cache'):
            return False
        
        try:
            await self._backend.warm_cache(operations)
            return True
        except Exception as e:
            log.error(f"Failed to warm cache: {e}")
            return False


# 全局存储适配器实例
_storage_adapter: Optional[StorageAdapter] = None


async def get_storage_adapter(
    enable_cache: Optional[bool] = None,
    cache_size: Optional[int] = None,
    cache_ttl: Optional[float] = None
) -> StorageAdapter:
    """获取全局存储适配器实例
    
    Args:
        enable_cache: 是否启用缓存，默认从环境变量ENABLE_STORAGE_CACHE获取，未设置时默认为True
        cache_size: 缓存大小，默认从环境变量STORAGE_CACHE_SIZE获取，未设置时默认为1000
        cache_ttl: 缓存TTL（秒），默认从环境变量STORAGE_CACHE_TTL获取，未设置时默认为300
    """
    global _storage_adapter
    
    if _storage_adapter is None:
        # 从环境变量或参数获取配置
        if enable_cache is None:
            enable_cache = os.getenv("ENABLE_STORAGE_CACHE", "true").lower() == "true"
        if cache_size is None:
            cache_size = int(os.getenv("STORAGE_CACHE_SIZE", "1000"))
        if cache_ttl is None:
            cache_ttl = float(os.getenv("STORAGE_CACHE_TTL", "300.0"))
        
        _storage_adapter = StorageAdapter(
            enable_cache=enable_cache,
            cache_size=cache_size,
            cache_ttl=cache_ttl
        )
        await _storage_adapter.initialize()
    
    return _storage_adapter


async def close_storage_adapter():
    """关闭全局存储适配器"""
    global _storage_adapter
    
    if _storage_adapter:
        await _storage_adapter.close()
        _storage_adapter = None