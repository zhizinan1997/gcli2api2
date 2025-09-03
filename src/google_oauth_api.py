"""
Google OAuth2 认证模块
"""
import time
import jwt
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List
from urllib.parse import urlencode
from cryptography.hazmat.primitives.serialization import load_pem_private_key
import httpx

from config import get_proxy_config, get_oauth_proxy_url, get_googleapis_proxy_url
from log import log


class TokenError(Exception):
    """Token相关错误"""
    pass


class Credentials:
    """凭证类"""
    
    def __init__(self, access_token: str, refresh_token: str = None,
                 client_id: str = None, client_secret: str = None,
                 expires_at: datetime = None, project_id: str = None):
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.expires_at = expires_at
        self.project_id = project_id
        
        # 获取反代配置
        self.oauth_base_url = get_oauth_proxy_url()
        self.token_endpoint = f"{self.oauth_base_url.rstrip('/')}/token"
    
    def is_expired(self) -> bool:
        """检查token是否过期"""
        if not self.expires_at:
            return True
        
        # 提前3分钟认为过期
        buffer = timedelta(minutes=3)
        return (self.expires_at - buffer) <= datetime.now(timezone.utc)
    
    async def refresh_if_needed(self) -> bool:
        """如果需要则刷新token"""
        if not self.is_expired():
            return False
        
        if not self.refresh_token:
            raise TokenError("需要刷新令牌但未提供")
        
        await self.refresh()
        return True
    
    async def refresh(self):
        """刷新访问令牌"""
        if not self.refresh_token:
            raise TokenError("无刷新令牌")
        
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'refresh_token': self.refresh_token,
            'grant_type': 'refresh_token'
        }
        
        # 获取代理配置
        proxy_config = get_proxy_config()
        client_kwargs = {}
        if proxy_config:
            client_kwargs["proxy"] = proxy_config
        
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )
                response.raise_for_status()
                
                token_data = response.json()
                self.access_token = token_data['access_token']
                
                if 'expires_in' in token_data:
                    expires_in = int(token_data['expires_in'])
                    self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                
                if 'refresh_token' in token_data:
                    self.refresh_token = token_data['refresh_token']
                
                log.debug(f"Token刷新成功，过期时间: {self.expires_at}")
                
            except httpx.HTTPStatusError as e:
                error_msg = f"Token刷新失败: {e.response.status_code}"
                if hasattr(e, 'response') and e.response.text:
                    error_msg += f" - {e.response.text}"
                log.error(error_msg)
                raise TokenError(error_msg)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Credentials':
        """从字典创建凭证"""
        # 处理过期时间
        expires_at = None
        if 'expiry' in data and data['expiry']:
            try:
                expiry_str = data['expiry']
                if isinstance(expiry_str, str):
                    if expiry_str.endswith('Z'):
                        expires_at = datetime.fromisoformat(expiry_str.replace('Z', '+00:00'))
                    elif '+' in expiry_str:
                        expires_at = datetime.fromisoformat(expiry_str)
                    else:
                        expires_at = datetime.fromisoformat(expiry_str).replace(tzinfo=timezone.utc)
            except ValueError:
                log.warning(f"无法解析过期时间: {expiry_str}")
        
        return cls(
            access_token=data.get('token') or data.get('access_token', ''),
            refresh_token=data.get('refresh_token'),
            client_id=data.get('client_id'),
            client_secret=data.get('client_secret'),
            expires_at=expires_at,
            project_id=data.get('project_id')
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """转为字典"""
        result = {
            'access_token': self.access_token,
            'refresh_token': self.refresh_token,
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'project_id': self.project_id
        }
        
        if self.expires_at:
            result['expiry'] = self.expires_at.isoformat()
        
        return result


class Flow:
    """OAuth流程类"""
    
    def __init__(self, client_id: str, client_secret: str, scopes: List[str],
                 redirect_uri: str = None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.scopes = scopes
        self.redirect_uri = redirect_uri
        
        # 获取反代配置
        self.oauth_base_url = get_oauth_proxy_url()
        self.token_endpoint = f"{self.oauth_base_url.rstrip('/')}/token"
        self.auth_endpoint = "https://accounts.google.com/o/oauth2/auth"
        
        self.credentials: Optional[Credentials] = None
    
    def get_auth_url(self, state: str = None, **kwargs) -> str:
        """生成授权URL"""
        params = {
            'client_id': self.client_id,
            'redirect_uri': self.redirect_uri,
            'scope': ' '.join(self.scopes),
            'response_type': 'code',
            'access_type': 'offline',
            'prompt': 'consent',
            'include_granted_scopes': 'true'
        }
        
        if state:
            params['state'] = state
        
        params.update(kwargs)
        return f"{self.auth_endpoint}?{urlencode(params)}"
    
    async def exchange_code(self, code: str) -> Credentials:
        """用授权码换取token"""
        data = {
            'client_id': self.client_id,
            'client_secret': self.client_secret,
            'redirect_uri': self.redirect_uri,
            'code': code,
            'grant_type': 'authorization_code'
        }
        
        # 获取代理配置
        proxy_config = get_proxy_config()
        client_kwargs = {}
        if proxy_config:
            client_kwargs["proxy"] = proxy_config
        
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )
                response.raise_for_status()
                
                token_data = response.json()
                
                # 计算过期时间
                expires_at = None
                if 'expires_in' in token_data:
                    expires_in = int(token_data['expires_in'])
                    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                
                # 创建凭证对象
                self.credentials = Credentials(
                    access_token=token_data['access_token'],
                    refresh_token=token_data.get('refresh_token'),
                    client_id=self.client_id,
                    client_secret=self.client_secret,
                    expires_at=expires_at
                )
                
                return self.credentials
                
            except httpx.HTTPStatusError as e:
                error_msg = f"获取token失败: {e.response.status_code}"
                if hasattr(e, 'response') and e.response.text:
                    error_msg += f" - {e.response.text}"
                log.error(error_msg)
                raise TokenError(error_msg)


class ServiceAccount:
    """Service Account类"""
    
    def __init__(self, email: str, private_key: str, project_id: str = None,
                 scopes: List[str] = None):
        self.email = email
        self.private_key = private_key
        self.project_id = project_id
        self.scopes = scopes or []
        
        # 获取反代配置
        self.oauth_base_url = get_oauth_proxy_url()
        self.token_endpoint = f"{self.oauth_base_url.rstrip('/')}/token"
        
        self.access_token: Optional[str] = None
        self.expires_at: Optional[datetime] = None
    
    def is_expired(self) -> bool:
        """检查token是否过期"""
        if not self.expires_at:
            return True
        
        buffer = timedelta(minutes=3)
        return (self.expires_at - buffer) <= datetime.now(timezone.utc)
    
    def create_jwt(self) -> str:
        """创建JWT令牌"""
        now = int(time.time())
        
        payload = {
            'iss': self.email,
            'scope': ' '.join(self.scopes) if self.scopes else '',
            'aud': self.token_endpoint,
            'exp': now + 3600,
            'iat': now
        }
        
        # 加载私钥
        private_key_obj = load_pem_private_key(
            self.private_key.encode('utf-8'),
            password=None
        )
        
        return jwt.encode(payload, private_key_obj, algorithm='RS256')
    
    async def get_access_token(self) -> str:
        """获取访问令牌"""
        if not self.is_expired() and self.access_token:
            return self.access_token
        
        assertion = self.create_jwt()
        
        data = {
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
            'assertion': assertion
        }
        
        # 获取代理配置
        proxy_config = get_proxy_config()
        client_kwargs = {}
        if proxy_config:
            client_kwargs["proxy"] = proxy_config
        
        async with httpx.AsyncClient(**client_kwargs) as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'}
                )
                response.raise_for_status()
                
                token_data = response.json()
                self.access_token = token_data['access_token']
                
                if 'expires_in' in token_data:
                    expires_in = int(token_data['expires_in'])
                    self.expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
                
                return self.access_token
                
            except httpx.HTTPStatusError as e:
                error_msg = f"Service Account获取token失败: {e.response.status_code}"
                if hasattr(e, 'response') and e.response.text:
                    error_msg += f" - {e.response.text}"
                log.error(error_msg)
                raise TokenError(error_msg)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], scopes: List[str] = None) -> 'ServiceAccount':
        """从字典创建Service Account凭证"""
        return cls(
            email=data['client_email'],
            private_key=data['private_key'],
            project_id=data.get('project_id'),
            scopes=scopes
        )


# 工具函数
async def get_user_info(credentials: Credentials) -> Optional[Dict[str, Any]]:
    """获取用户信息"""
    await credentials.refresh_if_needed()
    
    googleapis_base_url = get_googleapis_proxy_url()
    userinfo_url = f"{googleapis_base_url.rstrip('/')}/oauth2/v2/userinfo"
    
    # 获取代理配置  
    proxy_config = get_proxy_config()
    client_kwargs = {}
    if proxy_config:
        client_kwargs["proxy"] = proxy_config
    
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            response = await client.get(
                userinfo_url,
                headers={'Authorization': f'Bearer {credentials.access_token}'}
            )
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.error(f"获取用户信息失败: {e}")
            return None


async def validate_token(token: str) -> Optional[Dict[str, Any]]:
    """验证访问令牌"""
    oauth_base_url = get_oauth_proxy_url()
    tokeninfo_url = f"{oauth_base_url.rstrip('/')}/tokeninfo"
    
    # 获取代理配置  
    proxy_config = get_proxy_config()
    client_kwargs = {}
    if proxy_config:
        client_kwargs["proxy"] = proxy_config
    
    async with httpx.AsyncClient(**client_kwargs) as client:
        try:
            response = await client.get(f"{tokeninfo_url}?access_token={token}")
            response.raise_for_status()
            return response.json()
        except Exception as e:
            log.error(f"验证令牌失败: {e}")
            return None