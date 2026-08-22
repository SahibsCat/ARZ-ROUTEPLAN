from typing import List, Optional

from pydantic import BaseModel


class UploadResponse(BaseModel):
    message: str
    total_orders: int
    is_valid: bool
    errors: List[str]
    orders: List[dict]
    successful_orders: List[dict] = []
    failed_orders: List[dict] = []
    file_name: Optional[str] = None
    batch_id: Optional[int] = None
    column_order: List[dict] = []


class RouteItem(BaseModel):
    # Nullable/absent for a route straight out of route_service.generate_routes()
    # (the raw computation, before it's ever hit the DB) - always present once
    # it's been through crud.route_summary(), which is what every manual-edit
    # endpoint (add/remove/reorder order, in main.py) needs to target this
    # route at all. _process_route_generation rebuilds its response from the
    # persisted plan specifically so these are never missing on screen.
    route_id: Optional[int] = None
    route_name: str
    vehicle_type: str
    orders: List[dict]
    route_distance_km: Optional[float] = None
    route_time_minutes: Optional[float] = None
    number_of_stops: int = 0
    capacity: Optional[int] = None
    available_capacity: Optional[int] = None
    is_full: Optional[bool] = None
    max_capacity: Optional[int] = None
    available_max_capacity: Optional[int] = None
    is_at_max_capacity: Optional[bool] = None
    areas: List[str] = []
    route_segments: List[dict] = []
    google_maps_url: Optional[str] = None
    estimated_finish_time: Optional[str] = None
    average_stop_time: Optional[float] = None
    delivery_sequence: List[object] = []
    late_deliveries: List[object] = []
    utilization_percent: Optional[float] = None
    is_auto_created: bool = False
    status: Optional[str] = None


class RoutePlanResponse(BaseModel):
    route_count: int
    routes: List[RouteItem]
    pending_orders: List[dict]
    warnings: List[str] = []
    plan_id: Optional[int] = None
    is_saved: bool = False


class RetryGeocodeRequest(BaseModel):
    order_id: str
    updated_address: str
    orders: List[dict]
    available_cars: int = 1
    available_bikes: int = 2


class SettingsResponse(BaseModel):
    default_car_count: int
    default_bike_count: int
    theme: str
    preferences: dict = {}


class SettingsUpdateRequest(BaseModel):
    default_car_count: Optional[int] = None
    default_bike_count: Optional[int] = None
    theme: Optional[str] = None
    preferences: Optional[dict] = None


class DashboardResponse(BaseModel):
    has_data: bool
    batch_id: Optional[int] = None
    file_name: Optional[str] = None
    uploaded_at: Optional[str] = None
    total_orders: int = 0
    is_valid: bool = True
    errors: List[str] = []
    orders: List[dict] = []
    failed_orders: List[dict] = []
    routes: List[dict] = []
    pending_orders: List[dict] = []
    warnings: List[str] = []
    plan_id: Optional[int] = None
    plan_is_saved: bool = False
    plan_label: Optional[str] = None
    column_order: List[dict] = []
    settings: SettingsResponse


class RoutePlanSaveRequest(BaseModel):
    label: Optional[str] = None


class RoutePlanListItem(BaseModel):
    plan_id: int
    label: Optional[str] = None
    saved_at: Optional[str] = None
    created_at: Optional[str] = None
    batch_id: Optional[int] = None
    file_name: Optional[str] = None
    available_cars: int
    available_bikes: int
    route_count: int
    total_stops: int
    pending_count: int
    total_distance_km: float


class RoutePlanHistoryResponse(BaseModel):
    plans: List[RoutePlanListItem]
    total: int
    limit: int
    offset: int


class CreateRouteRequest(BaseModel):
    vehicle_type: str
    order_ids: List[str] = []


class AddOrdersRequest(BaseModel):
    order_ids: List[str]


class ReorderRouteRequest(BaseModel):
    order_ids: List[str]


class AssignOrdersRequest(BaseModel):
    order_ids: List[str]
    route_id: int


class ChangeVehicleTypeRequest(BaseModel):
    vehicle_type: str


class MoveOrdersRequest(BaseModel):
    source_route_id: int
    order_ids: List[str]


# --------------------------------------------------------------------------
# Drivers, assignment, live tracking
# --------------------------------------------------------------------------

class CreateDriverRequest(BaseModel):
    name: str
    username: str
    password: str
    mobile: Optional[str] = None
    vehicle_number: Optional[str] = None
    notes: Optional[str] = None


class UpdateDriverRequest(BaseModel):
    name: Optional[str] = None
    mobile: Optional[str] = None
    vehicle_number: Optional[str] = None
    notes: Optional[str] = None


class DriverStatusRequest(BaseModel):
    status: str


class ResetDriverPasswordRequest(BaseModel):
    new_password: str


class AssignDriverRequest(BaseModel):
    driver_id: int
    force: bool = False


class DriverLoginRequest(BaseModel):
    username: str
    password: str


class DriverLocationRequest(BaseModel):
    route_id: int
    lat: float
    lng: float
    speed: Optional[float] = None
    heading: Optional[float] = None
    accuracy: Optional[float] = None


class SetStopDeliveredRequest(BaseModel):
    delivered: bool = True
