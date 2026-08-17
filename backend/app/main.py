import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import NamedTemporaryFile
import traceback
from typing import Dict, List, Optional

from dotenv import load_dotenv

load_dotenv()

# Windows consoles default to the cp1252 codepage, which can't encode the
# emoji used in service-layer log lines - reconfigure stdout/stderr to UTF-8
# (falling back to "?" instead of crashing) so a print() never takes down a
# request.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from app import crud
from app.database import init_db
from app.dependencies import get_db
from app.excel_service import validate_excel_file
from app.geocode_service import geocode_orders
from app.route_service import generate_routes
from app.schemas import (
    DashboardResponse,
    RoutePlanHistoryResponse,
    RoutePlanResponse,
    RoutePlanSaveRequest,
    SettingsResponse,
    SettingsUpdateRequest,
    UploadResponse,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Routeplan API", lifespan=lifespan)

# CORS Configuration. Defaults to "*" (unchanged behavior) so this is a
# no-op everywhere it isn't set. On Render, frontend and backend are
# separate origins - set ALLOWED_ORIGINS to the frontend's Render URL
# (comma-separated if there's more than one, e.g. a custom domain too) to
# lock this down instead of leaving it wide open in production.
_origins_env = os.environ.get("ALLOWED_ORIGINS", "").strip()
origins = [o.strip() for o in _origins_env.split(",") if o.strip()] if _origins_env else ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def read_root():
    return {"message": "Routeplan backend is running"}


@app.get("/health")
def health_check():
    return {"status": "ok"}


def _safe_save_upload_batch(
    db: Session,
    file_name: Optional[str],
    total_orders: int,
    is_valid: bool,
    errors: List[str],
    geocoded_orders: List[Dict[str, object]],
    column_order: Optional[List[Dict[str, Optional[str]]]] = None,
) -> Optional[int]:
    """Persistence is additive - a DB write failure must never turn an
    otherwise-successful upload/route response into a 500."""
    try:
        batch = crud.save_upload_batch(db, file_name, total_orders, is_valid, errors, geocoded_orders, column_order=column_order)
        return batch.id
    except Exception:
        traceback.print_exc()
        db.rollback()
        return None


def _safe_save_route_plan(
    db: Session,
    batch_id: Optional[int],
    plan: Dict[str, object],
    available_cars: int,
    available_bikes: int,
) -> Optional[int]:
    try:
        route_plan = crud.save_route_plan(db, batch_id, plan, available_cars, available_bikes)
        return route_plan.id
    except Exception:
        traceback.print_exc()
        db.rollback()
        return None


def _safe_update_fleet_settings(db: Session, available_cars: int, available_bikes: int) -> None:
    """Whatever fleet size a route was last generated with becomes the new
    default - so a restart (or another user opening the app) starts from
    where this session left off instead of the hardcoded 1 car / 2 bikes."""
    try:
        crud.update_settings(db, default_car_count=available_cars, default_bike_count=available_bikes)
    except Exception:
        traceback.print_exc()
        db.rollback()


async def _process_upload(file: UploadFile, db: Session) -> UploadResponse:
    try:
        if not file.filename.lower().endswith(".xlsx"):
            return UploadResponse(
                message="Only .xlsx files are supported",
                total_orders=0,
                is_valid=False,
                errors=["The uploaded file must be an Excel .xlsx file"],
                file_name=file.filename,
            )

        with NamedTemporaryFile(suffix=".xlsx", delete=False) as temp_file:
            content = await file.read()
            temp_file.write(content)
            temp_path = temp_file.name

        result = validate_excel_file(temp_path)
        Path(temp_path).unlink(missing_ok=True)

        orders = result.get("orders", [])
        geocoded_orders, provider_error_message = geocode_orders(orders, db=db)
        failed_geocodes = [
            order
            for order in geocoded_orders
            if order.get("lat") is None or order.get("lng") is None
        ]
        successful_orders = [
            order
            for order in geocoded_orders
            if order.get("lat") is not None and order.get("lng") is not None
        ]

        if provider_error_message:
            # The geocoding PROVIDER is broken (billing/key/access) - every
            # address failed for the same reason, not because the data was
            # bad. Put this front and center instead of letting it look like
            # a data-quality problem.
            result["is_valid"] = False
            result["errors"].insert(0, provider_error_message)
        elif failed_geocodes and not successful_orders:
            result["is_valid"] = False
            result["errors"].append(
                f"{len(failed_geocodes)} address(es) failed geocoding."
            )

        message = (
            "Excel validation passed"
            if not failed_geocodes
            else f"Excel validation completed with {len(successful_orders)} orders ready"
        )

        column_order = result.get("column_order") or []
        batch_id = _safe_save_upload_batch(
            db,
            file.filename,
            result["total_orders"],
            result["is_valid"],
            result["errors"],
            geocoded_orders,
            column_order=column_order,
        )

        return UploadResponse(
            message=message,
            total_orders=result["total_orders"],
            is_valid=result["is_valid"],
            errors=result["errors"],
            orders=geocoded_orders,
            successful_orders=successful_orders,
            failed_orders=failed_geocodes,
            file_name=file.filename,
            batch_id=batch_id,
            column_order=column_order,
        )

    except Exception as e:
        traceback.print_exc()
        return UploadResponse(
            message="Upload failed",
            total_orders=0,
            is_valid=False,
            errors=[str(e)],
            orders=[],
            file_name=file.filename,
        )


@app.post("/upload-excel", response_model=UploadResponse)
async def upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    return await _process_upload(file, db)


@app.post("/api/orders/upload", response_model=UploadResponse)
async def api_upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    return await _process_upload(file, db)


def _process_route_generation(
    orders: List[Dict[str, object]],
    available_cars: int,
    available_bikes: int,
    batch_id: Optional[int],
    db: Session,
) -> RoutePlanResponse:
    plan = generate_routes(
        orders,
        available_cars=available_cars,
        available_bikes=available_bikes,
    )
    plan_id = _safe_save_route_plan(db, batch_id, plan, available_cars, available_bikes)
    _safe_update_fleet_settings(db, available_cars, available_bikes)
    # Always False here - a freshly generated plan is always a new unsaved
    # draft (save_route_plan just replaced any previous draft for this
    # batch with it). The user has to click Save to promote it.
    return RoutePlanResponse(**plan, plan_id=plan_id, is_saved=False)


@app.post("/generate-routes", response_model=RoutePlanResponse)
async def generate_route_plan(
    orders: List[Dict[str, object]] = Body(...),
    available_cars: int = Body(1),
    available_bikes: int = Body(2),
    batch_id: Optional[int] = Body(None),
    db: Session = Depends(get_db),
):
    return _process_route_generation(orders, available_cars, available_bikes, batch_id, db)


@app.post("/api/routes/generate", response_model=RoutePlanResponse)
async def api_generate_route_plan(
    orders: List[Dict[str, object]] = Body(...),
    available_cars: int = Body(1),
    available_bikes: int = Body(2),
    batch_id: Optional[int] = Body(None),
    db: Session = Depends(get_db),
):
    return _process_route_generation(orders, available_cars, available_bikes, batch_id, db)


@app.post("/api/geocode/retry")
async def retry_single_geocode(payload: Dict[str, object] = Body(...), db: Session = Depends(get_db)):
    order_id = str(payload.get("order_id", ""))
    updated_address = str(payload.get("updated_address", ""))
    orders_input = payload.get("orders", [])
    if not isinstance(orders_input, list):
        orders_input = []
    available_cars = int(payload.get("available_cars", 1))
    available_bikes = int(payload.get("available_bikes", 2))
    raw_batch_id = payload.get("batch_id")
    batch_id = int(raw_batch_id) if raw_batch_id is not None else None

    try:
        from app.geocode_service import geocode_single_address

        updated_orders = []

        for order in orders_input:
            item = dict(order)
            if str(item.get("order_id")) == order_id:
                target_address = updated_address if updated_address else str(item.get("address", ""))
                item["address"] = target_address
                res = geocode_single_address(target_address, db=db)
                if res:
                    item["lat"] = res["lat"]
                    item["lng"] = res["lng"]
                    item["geocoded_address"] = res["display_name"]
                    item["confidence"] = res.get("confidence")
                    item.pop("geocode_error", None)
                else:
                    item["lat"] = None
                    item["lng"] = None
                    item["geocode_error"] = "Address could not be geocoded"

                try:
                    crud.record_retry_attempt(
                        db, batch_id, order_id, updated_address or None,
                        success=res is not None,
                        failure_reason=item.get("geocode_error"),
                    )
                except Exception:
                    traceback.print_exc()
                    db.rollback()
            updated_orders.append(item)

        successful_orders = [
            o for o in updated_orders if o.get("lat") is not None and o.get("lng") is not None
        ]
        failed_orders = [
            o for o in updated_orders if o.get("lat") is None or o.get("lng") is None
        ]

        plan = generate_routes(
            successful_orders,
            available_cars=available_cars,
            available_bikes=available_bikes,
        )
        plan_id = _safe_save_route_plan(db, batch_id, plan, available_cars, available_bikes)
        _safe_update_fleet_settings(db, available_cars, available_bikes)

        return {
            "message": "Retry processed successfully",
            "total_orders": len(updated_orders),
            "is_valid": True,
            "orders": updated_orders,
            "successful_orders": successful_orders,
            "failed_orders": failed_orders,
            "routes": plan.get("routes", []),
            "pending_orders": plan.get("pending_orders", []),
            "warnings": plan.get("warnings", []),
            "plan_id": plan_id,
            "is_saved": False,
        }

    except Exception as e:
        traceback.print_exc()
        return {
            "message": f"Retry failed: {e}",
            "total_orders": len(orders_input),
            "is_valid": False,
            "orders": orders_input,
            "successful_orders": [],
            "failed_orders": [],
            "routes": [],
            "pending_orders": [],
            "warnings": [f"An unexpected error occurred while retrying: {e}"],
            "plan_id": None,
        }


@app.get("/api/dashboard", response_model=DashboardResponse)
def get_dashboard(db: Session = Depends(get_db)):
    """Called once when the app loads. If there's a current session in the
    database, the whole dashboard (orders, routes, pending, failed
    addresses, fleet defaults) is rebuilt from it - a refresh, a backend
    restart, or reopening the browser never loses data. If there's no
    current session, has_data is false and the frontend shows the normal
    upload-first empty state.

    "Current session" is an explicit pointer (app_settings.current_session_
    batch_id / current_session_plan_id), not "whichever upload happens to
    be newest in the whole table". That inferred version was a real bug:
    deleting your active upload/route made this fall through to an older,
    unrelated upload still sitting in history - a real file name and order
    count, just not the one you deleted, which reads exactly like "the
    delete didn't work" from the UI. The pointer is set on every
    upload/generate and cleared the moment that specific record is
    deleted, so an empty pointer means an honestly empty dashboard."""
    settings = crud.get_settings(db)
    settings_response = SettingsResponse(
        default_car_count=settings.default_car_count,
        default_bike_count=settings.default_bike_count,
        theme=settings.theme,
        preferences=settings.preferences or {},
    )

    batch = (
        crud.get_upload_batch(db, settings.current_session_batch_id)
        if settings.current_session_batch_id is not None else None
    )
    if batch is not None:
        route_plan = crud.get_latest_route_plan(db, batch_id=batch.id)
    elif settings.current_session_plan_id is not None:
        # A plan with no upload behind it (a manual/no-upload route
        # generation) is still a valid session on its own.
        route_plan = crud.get_route_plan(db, settings.current_session_plan_id)
    else:
        route_plan = None

    if batch is None and route_plan is None:
        return DashboardResponse(has_data=False, settings=settings_response)

    orders = [crud.order_summary(o) for o in batch.orders] if batch else []
    failed_orders = (
        [crud.failed_address_summary(f) for f in crud.list_failed_addresses(db, batch_id=batch.id)]
        if batch else []
    )
    plan_summary = crud.route_plan_summary(route_plan) if route_plan else {
        "routes": [], "pending_orders": [], "warnings": [], "plan_id": None,
        "is_saved": False, "label": None,
    }

    return DashboardResponse(
        has_data=True,
        batch_id=batch.id if batch else None,
        file_name=batch.file_name if batch else None,
        uploaded_at=batch.uploaded_at.isoformat() if batch and batch.uploaded_at else None,
        total_orders=batch.total_orders if batch else 0,
        is_valid=batch.is_valid if batch else True,
        errors=batch.errors or [] if batch else [],
        orders=orders,
        failed_orders=failed_orders,
        routes=plan_summary.get("routes", []),
        pending_orders=plan_summary.get("pending_orders", []),
        warnings=plan_summary.get("warnings", []),
        plan_id=plan_summary.get("plan_id"),
        plan_is_saved=plan_summary.get("is_saved", False),
        plan_label=plan_summary.get("label"),
        column_order=batch.column_order or [] if batch else [],
        settings=settings_response,
    )


@app.get("/api/settings", response_model=SettingsResponse)
def get_settings(db: Session = Depends(get_db)):
    settings = crud.get_settings(db)
    return SettingsResponse(
        default_car_count=settings.default_car_count,
        default_bike_count=settings.default_bike_count,
        theme=settings.theme,
        preferences=settings.preferences or {},
    )


@app.put("/api/settings", response_model=SettingsResponse)
def update_settings(payload: SettingsUpdateRequest = Body(...), db: Session = Depends(get_db)):
    settings = crud.update_settings(
        db,
        default_car_count=payload.default_car_count,
        default_bike_count=payload.default_bike_count,
        theme=payload.theme,
        preferences=payload.preferences,
    )
    return SettingsResponse(
        default_car_count=settings.default_car_count,
        default_bike_count=settings.default_bike_count,
        theme=settings.theme,
        preferences=settings.preferences or {},
    )


@app.get("/api/history")
def get_history(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    batches = crud.list_upload_batches(db, limit=limit, offset=offset)
    return {
        "batches": [crud.batch_summary(batch) for batch in batches],
        "total": crud.count_upload_batches(db),
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/history/{batch_id}")
def get_history_detail(batch_id: int, db: Session = Depends(get_db)):
    batch = crud.get_upload_batch(db, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Upload batch not found")
    return crud.batch_detail(batch)


@app.delete("/api/history/{batch_id}")
def delete_upload(batch_id: int, db: Session = Depends(get_db)):
    """Deletes the upload and everything derived from it (orders, route
    plans/routes/stops, failed addresses) - only ever called after the user
    confirms in the UI."""
    deleted = crud.delete_upload_batch(db, batch_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Upload batch not found")
    return {"message": "Upload deleted", "batch_id": batch_id}


@app.delete("/api/routes/{route_plan_id}")
def delete_route(route_plan_id: int, db: Session = Depends(get_db)):
    deleted = crud.delete_route_plan(db, route_plan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Route plan not found")
    return {"message": "Route plan deleted", "route_plan_id": route_plan_id}


@app.post("/api/routes/{route_plan_id}/save")
def save_route_to_history(
    route_plan_id: int,
    payload: RoutePlanSaveRequest = Body(default=RoutePlanSaveRequest()),
    db: Session = Depends(get_db),
):
    """Promotes a draft route plan to Route History. Nothing about the
    routes/stops themselves changes - only their is_saved flag - so this is
    a cheap, instant action from the user's point of view."""
    route_plan = crud.promote_route_plan_to_saved(db, route_plan_id, label=payload.label)
    if route_plan is None:
        raise HTTPException(status_code=404, detail="Route plan not found")
    return {
        "message": "Route plan saved to history",
        "plan_id": route_plan.id,
        "label": route_plan.label,
        "saved_at": route_plan.saved_at.isoformat() if route_plan.saved_at else None,
        "is_saved": True,
    }


@app.get("/api/routes/history", response_model=RoutePlanHistoryResponse)
def get_route_history(
    db: Session = Depends(get_db),
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    plans = crud.list_saved_route_plans(db, limit=limit, offset=offset)
    return RoutePlanHistoryResponse(
        plans=[crud.route_plan_list_item(plan) for plan in plans],
        total=crud.count_saved_route_plans(db),
        limit=limit,
        offset=offset,
    )


@app.get("/api/routes/history/{route_plan_id}")
def get_route_history_detail(route_plan_id: int, db: Session = Depends(get_db)):
    route_plan = crud.get_route_plan(db, route_plan_id)
    if route_plan is None:
        raise HTTPException(status_code=404, detail="Route plan not found")
    return crud.route_plan_summary(route_plan)


@app.get("/api/debug/geocode")
def debug_geocode():
    from app.geocode_service import geocode_address

    test_address = "123 MG Road, Bangalore"
    with httpx.Client(
        timeout=15,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://localhost/",
            "From": "rootplan@example.com",
        },
        follow_redirects=True,
    ) as client:
        result = geocode_address(test_address, client)

    return {
        "loaded": True,
        "test_address": test_address,
        "result": result,
    }
