"""Native conversion management; no request here starts or publishes a runtime."""

from fastapi import APIRouter, Depends, Query, Request

from nexus.contracts.agent_native import (
    NativeConversion,
    NativeConversionPage,
    NativeExecutionProfile,
    RegisterNativeConversion,
)
from nexus.server.api.v2.routers.agent_execution import actor, body
from nexus.server.api.v2.routers.agent_hosted import invoke

router = APIRouter(prefix="/native", tags=["product-agent-native"])


def service(request: Request):
    from nexus.server.api.v2.routers.product_agent_registry import service as registry_service

    return registry_service(request).native


@router.post("/conversions", response_model=NativeConversion)
async def register(request: Request, identity=Depends(actor), native=Depends(service)):
    await invoke(lambda: native._authorize(identity))
    command = await body(request, RegisterNativeConversion, limit=32000)
    return await invoke(lambda: native.register(identity, command))


@router.get("/conversions", response_model=NativeConversionPage)
async def list_conversions(
    asset_id: str = Query(..., max_length=150),
    after: str = Query("", max_length=150),
    identity=Depends(actor),
    native=Depends(service),
):
    return await invoke(lambda: native.list(identity, asset_id, after))


@router.get("/conversions/{conversion_id}", response_model=NativeConversion)
async def get_conversion(conversion_id: str, identity=Depends(actor), native=Depends(service)):
    return await invoke(lambda: native.get(identity, conversion_id))


@router.get("/conversions/{conversion_id}/profile", response_model=NativeExecutionProfile)
async def get_profile(conversion_id: str, identity=Depends(actor), native=Depends(service)):
    return await invoke(lambda: native.profile(identity, conversion_id))
