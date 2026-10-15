"""Feedback: ratings and comments per route."""
from flask import render_template

from .. import app
from ..db import execute, query
from ..web import opt, page_info, run, val


@app.route('/feedback')
def feedback():
    total = query('SELECT COUNT(*) AS n FROM Feedback')[0]['n']
    pg = page_info(total, 24)
    return render_template(
        'feedback.html', pg=pg,
        feedbacks=query("""SELECT F.*, P.Name, R.RouteName FROM Feedback F
                           JOIN Passenger P ON F.PassengerID = P.PassengerID
                           LEFT JOIN Route R ON F.RouteID = R.RouteID
                           ORDER BY F.FeedbackID DESC LIMIT %s OFFSET %s""", (pg['per_page'], pg['offset'])),
        passengers=query("SELECT PassengerID, Name FROM Passenger ORDER BY Name"),
        route_list=query("SELECT RouteID, RouteName FROM Route ORDER BY RouteName"),
        ratings=query("SELECT * FROM RouteRatings WHERE Reviews > 0 ORDER BY AvgRating DESC, Reviews DESC LIMIT 6"))


@app.route('/feedback/add', methods=['POST'])
def add_feedback():
    return run(lambda: execute(
        "INSERT INTO Feedback (PassengerID, RouteID, Rating, Comments) VALUES (%s,%s,%s,%s)",
        [val('passenger_id'), opt('route_id'), val('rating'), opt('comments')]),
        'Thanks! Feedback recorded.', 'feedback')


@app.route('/feedback/<int:fid>/delete', methods=['POST'])
def delete_feedback(fid):
    return run(lambda: execute("DELETE FROM Feedback WHERE FeedbackID=%s", (fid,)), 'Feedback deleted.', 'feedback')
