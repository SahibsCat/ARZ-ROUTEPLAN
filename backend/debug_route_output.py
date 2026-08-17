from app.route_service import generate_routes
orders = [
    {'order_id':'1003','customer_name':'Carol','address':'3 Main','delivery_time':'15:00'},
    {'order_id':'1001','customer_name':'Alice','address':'1 Main','delivery_time':'09:00'},
    {'order_id':'1002','customer_name':'Bob','address':'2 Main','delivery_time':'11:00'},
    {'order_id':'1004','customer_name':'Dina','address':'4 Main','delivery_time':'17:00'},
]
print(generate_routes(orders, available_cars=1, available_bikes=2))
