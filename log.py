"""
日志模块 - 支持从配置获取日志级别并实时输出到文件
"""
import sys
import threading
from datetime import datetime

# 日志系统启动时异步读取一次配置，不需要热更新
_log_level = "debug"
_log_file = "log.txt"
_config_initialized = False

async def init_log_config():
    """异步初始化日志配置（启动时调用一次）"""
    global _log_level, _log_file, _config_initialized
    if not _config_initialized:
        from config import get_log_level, get_log_file
        _log_level = await get_log_level()
        _log_file = await get_log_file()
        _config_initialized = True

def _get_log_level_sync():
    """获取日志级别"""
    return _log_level

def _get_log_file_sync():
    """获取日志文件路径"""
    return _log_file

# 日志级别定义
LOG_LEVELS = {
    'debug': 0,
    'info': 1,
    'warning': 2,
    'error': 3,
    'critical': 4
}

# 线程锁，用于文件写入同步
_file_lock = threading.Lock()

def _get_current_log_level():
    """获取当前日志级别"""
    try:
        level = _get_log_level_sync().lower()
        return LOG_LEVELS.get(level, LOG_LEVELS['info'])
    except:
        return LOG_LEVELS['info']

def _get_log_file_path():
    """获取日志文件路径"""
    try:
        return _get_log_file_sync()
    except:
        return "log.txt"

def _write_to_file(message: str):
    """线程安全地写入日志文件"""
    try:
        log_file = _get_log_file_path()
        with _file_lock:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(message + '\n')
                f.flush()  # 强制刷新到磁盘，确保实时写入
    except Exception as e:
        # 如果写文件失败，至少输出到控制台
        print(f"Warning: Failed to write to log file: {e}", file=sys.stderr)

def _log(level: str, message: str):
    """
    内部日志函数
    """
    level = level.lower()
    if level not in LOG_LEVELS:
        print(f"Warning: Unknown log level '{level}'", file=sys.stderr)
        return
    
    # 检查日志级别
    current_level = _get_current_log_level()
    if LOG_LEVELS[level] < current_level:
        return
    
    # 格式化日志消息
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = f"[{timestamp}] [{level.upper()}] {message}"
    
    # 输出到控制台
    if level in ('error', 'critical'):
        print(entry, file=sys.stderr)
    else:
        print(entry)
    
    # 实时写入文件
    _write_to_file(entry)

def set_log_level(level: str):
    """设置日志级别（注意：这只影响当前会话，不会持久化到配置）"""
    level = level.lower()
    if level not in LOG_LEVELS:
        print(f"Warning: Unknown log level '{level}'. Valid levels: {', '.join(LOG_LEVELS.keys())}")
        return False
    
    # 这里可以考虑动态更新配置，但目前只提供警告
    print(f"Note: To persist log level '{level}', please set LOG_LEVEL environment variable or update config.toml")
    return True

class Logger:
    """支持 log('info', 'msg') 和 log.info('msg') 两种调用方式"""
    
    def __call__(self, level: str, message: str):
        """支持 log('info', 'message') 调用方式"""
        _log(level, message)

    def debug(self, message: str):
        """记录调试信息"""
        _log('debug', message)
    
    def info(self, message: str):
        """记录一般信息"""
        _log('info', message)
    
    def warning(self, message: str):
        """记录警告信息"""
        _log('warning', message)
    
    def error(self, message: str):
        """记录错误信息"""
        _log('error', message)
    
    def critical(self, message: str):
        """记录严重错误信息"""
        _log('critical', message)
    
    def get_current_level(self) -> str:
        """获取当前日志级别名称"""
        current_level = _get_current_log_level()
        for name, value in LOG_LEVELS.items():
            if value == current_level:
                return name
        return 'info'
    
    def get_log_file(self) -> str:
        """获取当前日志文件路径"""
        return _get_log_file_path()

# 导出全局日志实例
log = Logger()

# 为了向后兼容，也导出日志级别设置函数
__all__ = ['log', 'set_log_level', 'LOG_LEVELS']