"""
High-performance credential manager with call-based rotation and caching.
Rotates credentials based on API call count rather than time for better quota distribution.
"""
import os
import json
import asyncio
import glob
import aiofiles
from datetime import datetime, timezone
from typing import Optional, List, Tuple
import httpx

from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleAuthRequest

from .config import CREDENTIALS_DIR, CODE_ASSIST_ENDPOINT
from .utils import get_user_agent, get_client_metadata
from log import log


class CredentialManager:
    """High-performance credential manager with call-based rotation and caching."""

    def __init__(self, calls_per_rotation: int = 10):  # Switch every 10 calls
        self._lock = asyncio.Lock()
        self._current_credential_index = 0
        self._credential_files: List[str] = []
        
        # Call-based rotation instead of time-based
        self._cached_credentials: Optional[Credentials] = None
        self._cached_project_id: Optional[str] = None
        self._call_count = 0
        self._calls_per_rotation = calls_per_rotation
        
        # Onboarding state
        self._onboarding_complete = False
        self._onboarding_checked = False
        
        # HTTP client reuse
        self._http_client: Optional[httpx.AsyncClient] = None
        
        self._initialized = False

    async def initialize(self):
        """Initialize the credential manager."""
        async with self._lock:
            if self._initialized:
                return
            
            await self._discover_credential_files()
            
            # Initialize HTTP client with connection pooling
            self._http_client = httpx.AsyncClient(
                timeout=30.0,
                limits=httpx.Limits(max_keepalive_connections=20, max_connections=100)
            )
            
            self._initialized = True

    async def close(self):
        """Clean up resources."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def _discover_credential_files(self):
        """Discover all credential files with hot reload support."""
        old_files = set(self._credential_files)
        self._credential_files = []
        
        credentials_dir = CREDENTIALS_DIR
        patterns = [os.path.join(credentials_dir, "*.json")]
        
        for pattern in patterns:
            self._credential_files.extend(glob.glob(pattern))
        
        self._credential_files = sorted(list(set(self._credential_files)))
        new_files = set(self._credential_files)
        
        # 检测文件变化
        if old_files != new_files:
            added_files = new_files - old_files
            removed_files = old_files - new_files
            
            if added_files:
                log.info(f"发现新的凭证文件: {list(added_files)}")
                # 清除缓存以便使用新文件
                self._cached_credentials = None
                self._cached_project_id = None
            
            if removed_files:
                log.info(f"凭证文件已移除: {list(removed_files)}")
                # 如果当前使用的文件被移除，切换到下一个文件
                if self._credential_files and self._current_credential_index < len(self._credential_files):
                    current_file = self._credential_files[self._current_credential_index]
                    if current_file in removed_files:
                        self._current_credential_index = 0
                        self._cached_credentials = None
                        self._cached_project_id = None
        
        if not self._credential_files:
            log.warning("No credential files found")
        else:
            log.info(f"Found {len(self._credential_files)} credential files")

    def _is_cache_valid(self) -> bool:
        """Check if cached credentials are still valid based on call count and token expiration."""
        if not self._cached_credentials:
            return False
        
        # 如果没有凭证文件，缓存无效
        if not self._credential_files:
            return False
        
        # Check if we've reached the rotation threshold
        if self._call_count >= self._calls_per_rotation:
            return False
        
        # Check token expiration (with 60 second buffer)
        if self._cached_credentials.expired:
            return False
        
        return True

    async def _rotate_credential_if_needed(self):
        """Rotate to next credential if call limit reached."""
        if self._call_count >= self._calls_per_rotation:
            self._current_credential_index = (self._current_credential_index + 1) % len(self._credential_files)
            self._call_count = 0  # Reset call counter
            log.info(f"Rotated to credential index {self._current_credential_index}")

    async def _load_credential_with_fallback(self, current_file: str) -> Tuple[Optional[Credentials], Optional[str]]:
        """Load credentials with fallback to next file on failure."""
        creds, project_id = await self._load_credentials_from_file(current_file)
        if not creds:
            # Try next file on failure
            self._current_credential_index = (self._current_credential_index + 1) % len(self._credential_files)
            if self._current_credential_index < len(self._credential_files):
                current_file = self._credential_files[self._current_credential_index]
                creds, project_id = await self._load_credentials_from_file(current_file)
        return creds, project_id

    async def get_credentials(self) -> Tuple[Optional[Credentials], Optional[str]]:
        """Get credentials with call-based rotation, caching and hot reload for performance."""
        async with self._lock:
            # 更积极的文件检查策略
            should_check_files = (
                self._call_count % 10 == 0 or  # 每10次调用检查一次（更频繁）
                not self._credential_files or  # 无文件时每次都检查
                not self._cached_credentials   # 无缓存时每次都检查
            )
            
            if should_check_files:
                await self._discover_credential_files()
            
            # Return cached credentials if valid and files exist
            if self._is_cache_valid() and self._credential_files:
                log.debug(f"Using cached credentials (call count: {self._call_count}/{self._calls_per_rotation})")
                return self._cached_credentials, self._cached_project_id
            
            # Cache miss or rotation needed - load fresh credentials
            if self._call_count >= self._calls_per_rotation:
                log.info(f"Rotating credentials after {self._call_count} calls")
            else:
                log.info("Cache miss - loading fresh credentials")
            
            # 确保文件列表是最新的
            await self._discover_credential_files()
            
            if not self._credential_files:
                log.error("No credential files available")
                return None, None
            
            # Rotate to next credential if we've reached the call limit
            await self._rotate_credential_if_needed()
            
            current_file = self._credential_files[self._current_credential_index]
            
            # Load credentials from file with fallback
            creds, project_id = await self._load_credential_with_fallback(current_file)
            
            if creds:
                # Update cache
                self._cached_credentials = creds
                self._cached_project_id = project_id
                log.info(f"Cached credentials from {current_file}")
            
            return creds, project_id

    async def _load_credentials_from_file(self, file_path: str) -> Tuple[Optional[Credentials], Optional[str]]:
        """Load credentials from file (optimized)."""
        try:
            async with aiofiles.open(file_path, "r") as f:
                content = await f.read()
            creds_data = json.loads(content)
            
            if "refresh_token" not in creds_data or not creds_data["refresh_token"]:
                log.warning(f"No refresh token in {file_path}")
                return None, None
            
            # Handle different credential formats
            if "access_token" in creds_data and "token" not in creds_data:
                creds_data["token"] = creds_data["access_token"]
            if "scope" in creds_data and "scopes" not in creds_data:
                creds_data["scopes"] = creds_data["scope"].split()
            
            # Handle expiry time format
            if "expiry" in creds_data and isinstance(creds_data["expiry"], str):
                try:
                    exp = creds_data["expiry"]
                    if "+00:00" in exp:
                        parsed = datetime.fromisoformat(exp)
                    elif exp.endswith("Z"):
                        parsed = datetime.fromisoformat(exp.replace('Z', '+00:00'))
                    else:
                        parsed = datetime.fromisoformat(exp)
                    ts = parsed.timestamp()
                    creds_data["expiry"] = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except Exception as e:
                    log.warning(f"Could not parse expiry in {file_path}: {e}")
                    del creds_data["expiry"]
            
            creds = Credentials.from_authorized_user_info(creds_data, creds_data.get("scopes"))
            project_id = creds_data.get("project_id")
            setattr(creds, "project_id", project_id)
            
            # Refresh if needed (but only once per cache cycle)
            if creds.expired and creds.refresh_token:
                try:
                    log.debug(f"Refreshing credentials from {file_path}")
                    creds.refresh(GoogleAuthRequest())
                except Exception as e:
                    log.warning(f"Failed to refresh credentials from {file_path}: {e}")
            
            return creds, project_id
        except Exception as e:
            log.error(f"Failed to load credentials from {file_path}: {e}")
            return None, None

    async def increment_call_count(self):
        """Increment the call count for tracking rotation."""
        async with self._lock:
            self._call_count += 1
            log.debug(f"Call count incremented to {self._call_count}/{self._calls_per_rotation}")

    async def rotate_to_next_credential(self):
        """Manually rotate to next credential (for error recovery)."""
        async with self._lock:
            # Invalidate cache
            self._cached_credentials = None
            self._cached_project_id = None
            self._call_count = 0  # Reset call count
            
            # Move to next credential
            self._current_credential_index = (self._current_credential_index + 1) % len(self._credential_files)
            log.info(f"Rotated to credential index {self._current_credential_index}")

    def get_user_project_id(self, creds: Credentials) -> str:
        """Get user project ID from credentials."""
        project_id = getattr(creds, "project_id", None)
        if project_id:
            return project_id

        raise Exception(
            "Unable to determine Google Cloud project ID. "
            "Ensure credential file contains project_id."
        )

    async def onboard_user(self, creds: Credentials, project_id: str):
        """Optimized user onboarding with caching."""
        # Skip if already onboarded for this session
        if self._onboarding_complete:
            return
        
        async with self._lock:
            # Double-check after acquiring lock
            if self._onboarding_complete:
                return
            
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(GoogleAuthRequest())
                except Exception as e:
                    raise Exception(f"Failed to refresh credentials during onboarding: {str(e)}")
            
            headers = {
                "Authorization": f"Bearer {creds.token}",
                "Content-Type": "application/json",
                "User-Agent": get_user_agent(),
            }
            
            load_assist_payload = {
                "cloudaicompanionProject": project_id,
                "metadata": get_client_metadata(project_id),
            }
            
            try:
                # Use reusable HTTP client
                resp = await self._http_client.post(
                    f"{CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
                    json=load_assist_payload,
                    headers=headers,
                )
                resp.raise_for_status()
                load_data = resp.json()
                
                # Determine tier
                tier = None
                if load_data.get("currentTier"):
                    tier = load_data["currentTier"]
                else:
                    for allowed_tier in load_data.get("allowedTiers", []):
                        if allowed_tier.get("isDefault"):
                            tier = allowed_tier
                            break
                    
                    if not tier:
                        tier = {
                            "name": "",
                            "description": "",
                            "id": "legacy-tier",
                            "userDefinedCloudaicompanionProject": True,
                        }

                if tier.get("userDefinedCloudaicompanionProject") and not project_id:
                    raise ValueError("This account requires setting the GOOGLE_CLOUD_PROJECT env var.")

                if load_data.get("currentTier"):
                    self._onboarding_complete = True
                    return

                # Onboard user
                onboard_req_payload = {
                    "tierId": tier.get("id"),
                    "cloudaicompanionProject": project_id,
                    "metadata": get_client_metadata(project_id),
                }

                while True:
                    onboard_resp = await self._http_client.post(
                        f"{CODE_ASSIST_ENDPOINT}/v1internal:onboardUser",
                        json=onboard_req_payload,
                        headers=headers,
                    )
                    onboard_resp.raise_for_status()
                    lro_data = onboard_resp.json()

                    if lro_data.get("done"):
                        self._onboarding_complete = True
                        break
                    
                    await asyncio.sleep(5)

            except httpx.HTTPStatusError as e:
                error_text = e.response.text if hasattr(e, 'response') else str(e)
                raise Exception(f"User onboarding failed. Please check your Google Cloud project permissions and try again. Error: {error_text}")
            except Exception as e:
                raise Exception(f"User onboarding failed due to an unexpected error: {str(e)}")

    async def refresh_credential_files(self):
        """手动刷新凭证文件列表（热加载）"""
        async with self._lock:
            log.info("手动刷新凭证文件列表")
            await self._discover_credential_files()
            # 清除缓存强制重新加载
            self._cached_credentials = None
            self._cached_project_id = None
            self._call_count = 0

    async def get_credentials_and_project(self) -> Tuple[Optional[Credentials], Optional[str]]:
        """Get both credentials and project ID in one optimized call."""
        if not self._initialized:
            await self.initialize()
        
        # 如果当前没有文件，强制检查一次
        if not self._credential_files:
            log.info("No credentials found, forcing file discovery")
            async with self._lock:
                await self._discover_credential_files()
        
        return await self.get_credentials()