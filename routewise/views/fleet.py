"""Fleet and drivers: vehicles (with their seat layout) and drivers, full CRUD for administrators."""
from flask import render_template

from .. import app
from ..db import execute, query
from ..web import opt, run, val


@app.route('/fleet')
def fleet():
    vehicles = query("""SELECT V.*, D.Name AS DriverName,
                               (SELECT COUNT(*) FROM Seat ST WHERE ST.VehicleID = V.VehicleID AND ST.Position = 'Window') AS WindowSeats
                        FROM Vehicle V LEFT JOIN Driver D ON V.DriverID = D.DriverID ORDER BY V.VehicleID""")
    drivers = query("""SELECT D.*, V.VehicleNo FROM Driver D
                       LEFT JOIN Vehicle V ON V.DriverID = D.DriverID ORDER BY D.DriverID""")
    return render_template('fleet.html', vehicles=vehicles, drivers=drivers, types=query('SELECT RouteType FROM FareRule ORDER BY RouteType'))


def vehicle_args():
    return [val('vehicle_no').upper(), val('type'), val('capacity'), val('seats_per_row') or 4, opt('model'),
            val('status') or 'Active', opt('driver_id')]


@app.route('/vehicles/add', methods=['POST'])
def add_vehicle():
    # the CreateSeats trigger builds the seat map of the new vehicle
    return run(lambda: execute(
        "INSERT INTO Vehicle (VehicleNo, Type, Capacity, SeatsPerRow, Model, Status, DriverID) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        vehicle_args()), 'Vehicle added.', 'fleet')


@app.route('/vehicles/<int:vid>/edit', methods=['POST'])
def edit_vehicle(vid):
    # the ResizeSeats trigger rebuilds the seat map, and refuses a change that would remove a booked seat
    return run(lambda: execute(
        "UPDATE Vehicle SET VehicleNo=%s, Type=%s, Capacity=%s, SeatsPerRow=%s, Model=%s, Status=%s, DriverID=%s WHERE VehicleID=%s",
        vehicle_args() + [vid]), 'Vehicle updated.', 'fleet')


@app.route('/vehicles/<int:vid>/delete', methods=['POST'])
def delete_vehicle(vid):
    return run(lambda: execute("DELETE FROM Vehicle WHERE VehicleID=%s", (vid,)), 'Vehicle deleted.', 'fleet')


@app.route('/drivers/add', methods=['POST'])
def add_driver():
    return run(lambda: execute(
        "INSERT INTO Driver (Name, LicenseNo, ShiftHours, Phone) VALUES (%s,%s,%s,%s)",
        [val('name'), val('license_no').upper(), val('shift_hours') or 8, opt('phone')]),
        f'Driver {val("name")} added.', 'fleet')


@app.route('/drivers/<int:did>/delete', methods=['POST'])
def delete_driver(did):
    return run(lambda: execute("DELETE FROM Driver WHERE DriverID=%s", (did,)), 'Driver deleted.', 'fleet')
