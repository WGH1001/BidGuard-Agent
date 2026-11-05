@echo off
chcp 65001 >nul
title BidGuard 标书体检系统
cd /d E:\agent\BidGuard-Agent
echo ============================================
echo   BidGuard 标书防废标体检系统 启动中...
echo   启动后浏览器打开: http://127.0.0.1:8000
echo   局域网/公网穿透映射端口: 8000
echo   停止服务: 关闭本窗口或按 Ctrl+C
echo ============================================
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port 8000
pause
