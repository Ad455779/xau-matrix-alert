import asyncio
import logging
import os
from collections import deque
from datetime import datetime, timezone
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
