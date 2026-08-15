"""
Chart Drawings — Phase 3B upgrade feature.

Persists the drawing-tool objects created on the live chart (trendlines,
horizontal/vertical lines, rectangles, fibonacci, brush strokes, notes,
etc.) so they survive a page refresh. Scoped to:

    user + symbol + timeframe

so BTC/USDT on the 1H chart keeps a separate drawing set from BTC/USDT on
the 4H chart, and from ETH/USDT entirely. Does not touch candles, signals,
or any other existing table/route.

Routes:
    GET    /api/chart/drawings?symbol=...&timeframe=...     -> list
    POST   /api/chart/drawings                               -> create one
    PATCH  /api/chart/drawings/<id>                           -> update one
    DELETE /api/chart/drawings/<id>                           -> delete one
    DELETE /api/chart/drawings?symbol=...&timeframe=...       -> clear all
                                                                  for that
                                                                  symbol+tf
"""

import json

from flask import Blueprint, request, jsonify, session

from db import get_db_connection

chart_drawings_bp = Blueprint("chart_drawings", __name__)

# Keep in sync with the tool ids in static/script.js (TOOL_ARITY / TOOL_ICONS).
# Anything not in this set is rejected rather than trusted blindly.
ALLOWED_DRAWING_TYPES = {
    "horizontal", "vertical", "hray", "crossline", "text", "note", "icon",
    "trendline", "ray", "extended", "trendangle", "rectangle", "ellipse",
    "arrow", "measure", "fib", "fibtimezone", "fibfan", "fibcircles",
    "fibspiral", "fibarcs", "gannbox", "longpos", "shortpos",
    "pricerange", "daterange", "callout", "fibext", "fibchannel",
    "fibwedge", "pitchfork", "triangle", "brush", "path",
    "support_zone", "resistance_zone",
}

MAX_DRAWING_DATA_BYTES = 20_000  # generous ceiling for a single drawing's coords
MAX_DRAWINGS_PER_SCOPE = 500     # per user+symbol+timeframe, guards against abuse


def _require_login():
    return session.get("user_id")


def _clean_symbol(raw):
    return (raw or "").strip().upper()[:30]


def _clean_timeframe(raw):
    return (raw or "").strip().lower()[:10]


def _row_to_json(row):
    try:
        data = json.loads(row["drawing_data"])
    except (TypeError, ValueError):
        data = {}
    style = None
    if row.get("style_data"):
        try:
            style = json.loads(row["style_data"])
        except (TypeError, ValueError):
            style = None
    out = dict(data)
    out["id"] = row["id"]
    out["type"] = row["drawing_type"]
    if style:
        out["_style"] = style
    return out


@chart_drawings_bp.route("/api/chart/drawings", methods=["GET"])
def list_drawings():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    symbol = _clean_symbol(request.args.get("symbol"))
    timeframe = _clean_timeframe(request.args.get("timeframe"))
    if not symbol or not timeframe:
        return jsonify({"error": "symbol and timeframe are required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT id, drawing_type, drawing_data, style_data FROM chart_drawings "
                "WHERE user_id = %s AND symbol = %s AND timeframe = %s ORDER BY id ASC",
                (user_id, symbol, timeframe),
            )
            rows = cursor.fetchall()
    except Exception as e:
        return jsonify({"error": "Could not load drawings", "detail": str(e)}), 500
    finally:
        conn.close()

    return jsonify({"drawings": [_row_to_json(r) for r in rows]})


@chart_drawings_bp.route("/api/chart/drawings", methods=["POST"])
def create_drawing():
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    symbol = _clean_symbol(body.get("symbol"))
    timeframe = _clean_timeframe(body.get("timeframe"))
    drawing_type = (body.get("type") or "").strip()
    coordinates = body.get("data")
    style = body.get("style")

    if not symbol or not timeframe:
        return jsonify({"error": "symbol and timeframe are required"}), 400
    if drawing_type not in ALLOWED_DRAWING_TYPES:
        return jsonify({"error": "Unknown or unsupported drawing type"}), 400
    if not isinstance(coordinates, dict):
        return jsonify({"error": "data must be an object of coordinates"}), 400

    try:
        drawing_json = json.dumps(coordinates)
    except (TypeError, ValueError):
        return jsonify({"error": "data is not serializable"}), 400
    if len(drawing_json) > MAX_DRAWING_DATA_BYTES:
        return jsonify({"error": "Drawing data too large"}), 400

    style_json = None
    if style is not None:
        if not isinstance(style, dict):
            return jsonify({"error": "style must be an object"}), 400
        try:
            style_json = json.dumps(style)
        except (TypeError, ValueError):
            return jsonify({"error": "style is not serializable"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS c FROM chart_drawings "
                "WHERE user_id = %s AND symbol = %s AND timeframe = %s",
                (user_id, symbol, timeframe),
            )
            if cursor.fetchone()["c"] >= MAX_DRAWINGS_PER_SCOPE:
                return jsonify({"error": "Drawing limit reached for this chart"}), 400

            cursor.execute(
                "INSERT INTO chart_drawings "
                "(user_id, symbol, timeframe, drawing_type, drawing_data, style_data) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (user_id, symbol, timeframe, drawing_type, drawing_json, style_json),
            )
            new_id = cursor.lastrowid
        return jsonify({"id": new_id, "message": "Drawing saved"}), 201
    except Exception as e:
        return jsonify({"error": "Could not save drawing", "detail": str(e)}), 500
    finally:
        conn.close()


@chart_drawings_bp.route("/api/chart/drawings/<int:drawing_id>", methods=["PATCH"])
def update_drawing(drawing_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify({"error": "Invalid JSON body"}), 400

    coordinates = body.get("data")
    style = body.get("style")
    if coordinates is None and style is None:
        return jsonify({"error": "Nothing to update"}), 400

    sets, params = [], []

    if coordinates is not None:
        if not isinstance(coordinates, dict):
            return jsonify({"error": "data must be an object of coordinates"}), 400
        try:
            drawing_json = json.dumps(coordinates)
        except (TypeError, ValueError):
            return jsonify({"error": "data is not serializable"}), 400
        if len(drawing_json) > MAX_DRAWING_DATA_BYTES:
            return jsonify({"error": "Drawing data too large"}), 400
        sets.append("drawing_data = %s")
        params.append(drawing_json)

    if style is not None:
        if not isinstance(style, dict):
            return jsonify({"error": "style must be an object"}), 400
        try:
            style_json = json.dumps(style)
        except (TypeError, ValueError):
            return jsonify({"error": "style is not serializable"}), 400
        sets.append("style_data = %s")
        params.append(style_json)

    params.extend([drawing_id, user_id])

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            # user_id is always included in the WHERE clause — a user can
            # never update a drawing that isn't theirs, regardless of what
            # id the frontend sends.
            cursor.execute(
                f"UPDATE chart_drawings SET {', '.join(sets)} WHERE id = %s AND user_id = %s",
                tuple(params),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Drawing not found"}), 404
        return jsonify({"message": "Updated"}), 200
    except Exception as e:
        return jsonify({"error": "Could not update drawing", "detail": str(e)}), 500
    finally:
        conn.close()


@chart_drawings_bp.route("/api/chart/drawings/<int:drawing_id>", methods=["DELETE"])
def delete_drawing(drawing_id):
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM chart_drawings WHERE id = %s AND user_id = %s",
                (drawing_id, user_id),
            )
            if cursor.rowcount == 0:
                return jsonify({"error": "Drawing not found"}), 404
        return jsonify({"message": "Deleted"}), 200
    except Exception as e:
        return jsonify({"error": "Could not delete drawing", "detail": str(e)}), 500
    finally:
        conn.close()


@chart_drawings_bp.route("/api/chart/drawings", methods=["DELETE"])
def clear_drawings():
    """Clear-all for the current user, scoped to one symbol+timeframe only —
    never touches other symbols/timeframes or other users' drawings."""
    user_id = _require_login()
    if not user_id:
        return jsonify({"error": "Not logged in"}), 401

    symbol = _clean_symbol(request.args.get("symbol"))
    timeframe = _clean_timeframe(request.args.get("timeframe"))
    if not symbol or not timeframe:
        return jsonify({"error": "symbol and timeframe are required"}), 400

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM chart_drawings WHERE user_id = %s AND symbol = %s AND timeframe = %s",
                (user_id, symbol, timeframe),
            )
            removed = cursor.rowcount
        return jsonify({"message": "Cleared", "removed": removed}), 200
    except Exception as e:
        return jsonify({"error": "Could not clear drawings", "detail": str(e)}), 500
    finally:
        conn.close()
