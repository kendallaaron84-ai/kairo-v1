from fastapi import APIRouter

from app.api.v1 import capital, research, system

router = APIRouter()
router.include_router(system.router, prefix="/system", tags=["system"])
router.include_router(research.router, prefix="/research", tags=["research"])
router.include_router(capital.router, prefix="/capital", tags=["capital"])
