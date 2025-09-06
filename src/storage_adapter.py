"""
存储适配器，提供统一的接口来处理本地文件存储和MongoDB存储。
根据配置自动选择存储后端。
"""
import asyncio
import os
import json
import time
from typing import Dict, Any, List, Optional, Protocol, TYPE_CHECKING

if TYPE_CHECKING:
    pass

import aiofiles
import toml

from log import log


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
    
    def __init__(self):
        self._backend: Optional["StorageBackend"] = None
        self._initialized = False
        self._lock = asyncio.Lock()
    
    async def initialize(self) -> None:
        """初始化存储适配器"""
        async with self._lock:
            if self._initialized:
                return
            
            # 检查是否配置了MongoDB（初始化时直接使用环境变量，避免循环依赖）
            mongodb_uri = os.getenv("MONGODB_URI", "")
            
            if mongodb_uri:
                # 使用MongoDB存储
                try:
                    from .mongodb_manager import MongoDBManager
                    self._backend = MongoDBManager()
                    await self._backend.initialize()
                    log.info("Using MongoDB storage backend")
                except ImportError as e:
                    log.error(f"Failed to import MongoDB backend: {e}")
                    log.info("Falling back to file storage backend")
                    self._backend = FileStorageBackend()
                    await self._backend.initialize()
                except Exception as e:
                    log.error(f"Failed to initialize MongoDB backend: {e}")
                    log.info("Falling back to file storage backend")
                    self._backend = FileStorageBackend()
                    await self._backend.initialize()
            else:
                # 使用文件存储
                self._backend = FileStorageBackend()
                await self._backend.initialize()
                log.info("Using file storage backend")
            
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
            "initialized": self._initialized
        }
        
        # 获取底层存储信息
        if hasattr(self._backend, 'get_database_info'):
            try:
                db_info = await self._backend.get_database_info()
                info.update(db_info)
            except Exception as e:
                info["database_error"] = str(e)
        elif isinstance(self._backend, FileStorageBackend):
            info.update({
                "credentials_dir": getattr(self._backend, '_credentials_dir', None),
                "state_file": getattr(self._backend, '_state_file', None),
                "config_file": getattr(self._backend, '_config_file', None)
            })
        
        return info


# 全局存储适配器实例
_storage_adapter: Optional[StorageAdapter] = None


async def get_storage_adapter() -> StorageAdapter:
    """获取全局存储适配器实例"""
    global _storage_adapter
    
    if _storage_adapter is None:
        _storage_adapter = StorageAdapter()
        await _storage_adapter.initialize()
    
    return _storage_adapter


async def close_storage_adapter():
    """关闭全局存储适配器"""
    global _storage_adapter
    
    if _storage_adapter:
        await _storage_adapter.close()
        _storage_adapter = None