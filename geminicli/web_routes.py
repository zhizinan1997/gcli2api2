"""
Web路由模块 - 处理认证相关的HTTP请求
用于与上级web.py集成
"""
import os
import logging
from typing import List
from fastapi import APIRouter, HTTPException, Request, Depends, File, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

from .auth_api import (
    create_auth_url, get_auth_status,
    verify_password, generate_auth_token, verify_auth_token,
    batch_upload_credentials, asyncio_complete_auth_flow,
)

# 创建路由器
router = APIRouter()
security = HTTPBearer()

class LoginRequest(BaseModel):
    password: str

class AuthStartRequest(BaseModel):
    project_id: str

class AuthCallbackRequest(BaseModel):
    project_id: str


def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """验证认证令牌"""
    if not verify_auth_token(credentials.credentials):
        raise HTTPException(status_code=401, detail="无效的认证令牌")
    return credentials.credentials


@router.get("/auth", response_class=HTMLResponse)
async def serve_auth_page():
    """提供认证页面"""
    try:
        # 读取HTML文件
        html_file_path = os.path.join(os.path.dirname(__file__), "auth_web.html")
        with open(html_file_path, "r", encoding="utf-8") as f:
            html_content = f.read()
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="认证页面不存在")
    except Exception as e:
        logging.error(f"加载认证页面失败: {e}")
        raise HTTPException(status_code=500, detail="服务器内部错误")


@router.post("/auth/login")
async def login(request: LoginRequest):
    """用户登录"""
    try:
        if verify_password(request.password):
            token = generate_auth_token()
            return JSONResponse(content={"token": token, "message": "登录成功"})
        else:
            raise HTTPException(status_code=401, detail="密码错误")
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"登录失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/start")
async def start_auth(request: AuthStartRequest, token: str = Depends(verify_token)):
    """开始认证流程"""
    try:
        if not request.project_id:
            raise HTTPException(status_code=400, detail="Project ID 不能为空")
        
        result = create_auth_url(request.project_id)
        
        if result['success']:
            return JSONResponse(content={
                "auth_url": result['auth_url'],
                "state": result['state']
            })
        else:
            raise HTTPException(status_code=500, detail=result['error'])
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"开始认证流程失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/callback")
async def auth_callback(request: AuthCallbackRequest, token: str = Depends(verify_token)):
    """处理认证回调（异步等待）"""
    try:
        if not request.project_id:
            raise HTTPException(status_code=400, detail="Project ID 不能为空")
        
        # 异步等待OAuth回调完成
        result = await asyncio_complete_auth_flow(request.project_id)
        
        if result['success']:
            return JSONResponse(content={
                "credentials": result['credentials'],
                "file_path": result['file_path'],
                "message": "认证成功，凭证已保存"
            })
        else:
            raise HTTPException(status_code=400, detail=result['error'])
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"处理认证回调失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/auth/status/{project_id}")
async def check_auth_status(project_id: str, token: str = Depends(verify_token)):
    """检查认证状态"""
    try:
        if not project_id:
            raise HTTPException(status_code=400, detail="Project ID 不能为空")
        
        status = get_auth_status(project_id)
        return JSONResponse(content=status)
        
    except Exception as e:
        logging.error(f"检查认证状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/auth/upload")
async def upload_credentials(files: List[UploadFile] = File(...), token: str = Depends(verify_token)):
    """批量上传认证文件"""
    try:
        if not files:
            raise HTTPException(status_code=400, detail="请选择要上传的文件")
        
        files_data = []
        for file in files:
            if not file.filename.endswith('.json'):
                raise HTTPException(status_code=400, detail=f"文件 {file.filename} 不是JSON格式")
            
            content = await file.read()
            try:
                content_str = content.decode('utf-8')
            except UnicodeDecodeError:
                raise HTTPException(status_code=400, detail=f"文件 {file.filename} 编码格式不支持")
            
            files_data.append({
                'filename': file.filename,
                'content': content_str
            })
        
        result = batch_upload_credentials(files_data)
        
        if result['uploaded_count'] > 0:
            return JSONResponse(content={
                "uploaded_count": result['uploaded_count'],
                "total_count": result['total_count'],
                "results": result['results'],
                "message": f"成功上传 {result['uploaded_count']}/{result['total_count']} 个文件"
            })
        else:
            raise HTTPException(status_code=400, detail="没有文件上传成功")
            
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"批量上传失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))