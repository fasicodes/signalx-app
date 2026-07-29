```python
# ============================================================
# /liquidity
# ============================================================
# LIVE LIQUIDITY SCANNER
#
# Primary:
#   Binance USD-M Futures
#
# Fallback:
#   Binance Spot
#
# IMPORTANT:
# Agar Railway se Binance Futures 451 return kare,
# scanner Binance Spot order book par fallback karega.
#
# Futures-only metrics:
#   funding_rate_pct
#   open_interest_usd
#
# Spot fallback mein ye values None hongi.
# ============================================================

@app.route("/liquidity", methods=["GET"])
def liquidity_endpoint():

    coin = request.args.get(
        "coin",
        "BTC/USDT"
    ).strip().upper()

    # --------------------------------------------------------
    # Convert:
    # BTC/USDT -> BTCUSDT
    # BTC-USDT -> BTCUSDT
    # BTCUSDT -> BTCUSDT
    # --------------------------------------------------------
    symbol = (
        coin
        .replace("/", "")
        .replace("-", "")
        .replace("_", "")
        .replace(" ", "")
        .upper()
    )

    try:

        # ----------------------------------------------------
        # GET LIQUIDITY DATA
        # Futures first, Spot fallback
        # ----------------------------------------------------
        data = get_liquidity_data(symbol)

        ticker = data["ticker"]
        mark = data["mark"]
        oi = data["oi"]
        depth = data["depth"]
        source = data["source"]

        # ----------------------------------------------------
        # VALIDATE TICKER
        # ----------------------------------------------------
        if not ticker or "lastPrice" not in ticker:
            return jsonify({
                "error": f"No live Binance data available for {symbol}",
                "symbol": symbol,
                "source": source,
            }), 200

        # ----------------------------------------------------
        # BASIC MARKET DATA
        # ----------------------------------------------------
        price = float(
            ticker.get("lastPrice", 0)
        )

        high_24h = float(
            ticker.get("highPrice", 0)
        )

        low_24h = float(
            ticker.get("lowPrice", 0)
        )

        volume_usd_24h = float(
            ticker.get("quoteVolume", 0)
        )

        change_pct_24h = float(
            ticker.get("priceChangePercent", 0)
        )

        # ----------------------------------------------------
        # FUTURES-ONLY DATA
        # ----------------------------------------------------
        mark_price = None
        funding_rate_pct = None
        open_interest_usd = None

        if source == "BINANCE_FUTURES":

            if mark and "markPrice" in mark:
                mark_price = float(
                    mark["markPrice"]
                )

            if mark and "lastFundingRate" in mark:
                funding_rate_pct = (
                    float(mark["lastFundingRate"])
                    * 100
                )

            if oi and "openInterest" in oi:
                open_interest_usd = (
                    float(oi["openInterest"])
                    * price
                )

        # ----------------------------------------------------
        # ORDER BOOK
        # ----------------------------------------------------
        bids = depth.get(
            "bids",
            []
        )

        asks = depth.get(
            "asks",
            []
        )

        if not bids or not asks:
            return jsonify({
                "error": f"No order-book data available for {symbol}",
                "symbol": symbol,
                "source": source,
            }), 200

        # ----------------------------------------------------
        # BUY / SELL LIQUIDITY BIAS
        # ----------------------------------------------------
        buy_pct, sell_pct = compute_liquidity_bias(
            bids,
            asks,
            levels=50
        )

        # ----------------------------------------------------
        # LARGEST WALLS
        # ----------------------------------------------------
        bid_wall = largest_liquidity_wall(
            bids,
            "BID"
        )

        ask_wall = largest_liquidity_wall(
            asks,
            "ASK"
        )

        # ----------------------------------------------------
        # SPOOFING HEURISTIC
        # ----------------------------------------------------
        history = LIQUIDITY_WALL_HISTORY[
            symbol
        ]

        spoof_flags = detect_liquidity_spoof(
            list(history),
            [
                bid_wall,
                ask_wall
            ]
        )

        # Save current walls
        if bid_wall:
            history.append(bid_wall)

        if ask_wall:
            history.append(ask_wall)

        # ----------------------------------------------------
        # LIQUIDITY BIAS
        # ----------------------------------------------------
        if buy_pct > 55:
            bias_tag = "BULLISH"

        elif sell_pct > 55:
            bias_tag = "BEARISH"

        else:
            bias_tag = "NEUTRAL"

        # ----------------------------------------------------
        # RESPONSE
        # ----------------------------------------------------
        return jsonify({

            "success": True,

            "symbol": symbol,

            "coin": coin,

            # Binance Futures OR Spot
            "data_source": source,

            # Price
            "price": round(
                price,
                8
            ),

            "high_24h": round(
                high_24h,
                8
            ),

            "low_24h": round(
                low_24h,
                8
            ),

            # 24h stats
            "volume_usd_24h": round(
                volume_usd_24h,
                2
            ),

            "change_pct_24h": round(
                change_pct_24h,
                2
            ),

            # Futures-only
            "mark_price": (
                round(
                    mark_price,
                    8
                )
                if mark_price is not None
                else None
            ),

            "funding_rate_pct": (
                round(
                    funding_rate_pct,
                    4
                )
                if funding_rate_pct is not None
                else None
            ),

            "open_interest_usd": (
                round(
                    open_interest_usd,
                    2
                )
                if open_interest_usd is not None
                else None
            ),

            # Order book
            "buy_pct": buy_pct,

            "sell_pct": sell_pct,

            "bias_tag": bias_tag,

            # Walls
            "bid_wall": bid_wall,

            "ask_wall": ask_wall,

            # Spoofing heuristic
            "spoof_flags": spoof_flags,

            # Server
            "server_time": int(
                time.time()
            ),

            # Useful status
            "fallback_used": (
                source != "BINANCE_FUTURES"
            ),

            "futures_error": (
                data.get("futures_error")
                if source != "BINANCE_FUTURES"
                else None
            ),

            "disclaimer": (
                "Liquidity data is display-only. "
                "Primary source is Binance USD-M Futures. "
                "If Futures is unavailable, Binance Spot "
                "order-book data is used as fallback. "
                "Funding rate and open interest are unavailable "
                "during Spot fallback. "
                "Liquidity bias, wall and spoofing heuristics "
                "are illustrative only and do not affect the "
                "/signal verdict or confidence."
            ),
        })

    except Exception as e:

        # ----------------------------------------------------
        # IMPORTANT:
        # Return 200 with success=False instead of 400.
        #
        # This prevents frontend console from repeatedly
        # showing HTTP 400 for a temporary external API issue.
        # ----------------------------------------------------
        return jsonify({

            "success": False,

            "error": str(e),

            "symbol": symbol,

            "coin": coin,

            "data_source": "UNAVAILABLE",

            "fallback_used": False,

            "server_time": int(
                time.time()
            ),

        }), 200
```
