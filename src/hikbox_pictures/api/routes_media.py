from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from hikbox_pictures.services.media_preview_service import MediaRangeError

router = APIRouter()


def _build_stream_response(payload) -> StreamingResponse:
    return StreamingResponse(
        payload.iter_bytes(),
        status_code=payload.status_code,
        media_type=payload.media_type,
        headers=payload.headers,
    )


@router.get("/photos/{photo_id}/original")
def get_original(photo_id: int, request: Request) -> StreamingResponse:
    try:
        payload = request.app.state.media_preview_service.read_original_stream(
            photo_id,
            range_header=request.headers.get("Range"),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except MediaRangeError as exc:
        raise HTTPException(
            status_code=416,
            detail="无效的 Range 请求",
            headers={"Content-Range": f"bytes */{exc.total_size}"},
        ) from exc

    return _build_stream_response(payload)


@router.get("/photos/{photo_id}/preview")
def get_preview(photo_id: int, request: Request) -> StreamingResponse:
    try:
        payload = request.app.state.media_preview_service.read_preview_stream(photo_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _build_stream_response(payload)


@router.get("/observations/{observation_id}/crop")
def get_observation_crop(observation_id: int, request: Request) -> StreamingResponse:
    try:
        payload = request.app.state.media_preview_service.read_observation_crop(observation_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _build_stream_response(payload)


@router.get("/observations/{observation_id}/context")
def get_observation_context(observation_id: int, request: Request) -> StreamingResponse:
    try:
        payload = request.app.state.media_preview_service.read_observation_context(observation_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return _build_stream_response(payload)
