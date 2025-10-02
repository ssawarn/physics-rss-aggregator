    # baseline
    baseline = None
    if PREFETCH and (time.time() - PREFETCH_TS) < PREFETCH_INTERVAL_SECONDS * 1.5:
        # direct hit
        baseline = PREFETCH.get(topic_key)
        if not baseline:
            # try a loose match across prefetched topics
            key_can = canonical(topic_key)
            for k, payload in PREFETCH.items():
                if canonical(k) in key_can or key_can in canonical(k):
                    baseline = payload
                    break

    if baseline:
        # Use prefetched items but apply 2-month filter again (fresh cutoff)
        grouped = filter_and_group_recent(baseline.get("items", []))
        payload = {
            "topic": baseline.get("topic", topic_key),
            "count": sum(len(v) for v in grouped.values()),
            "items": sum(grouped.values(), []),   # flattened list for backward compat
            "recent_grouped": grouped,
        }
    else:
        try:
            items = await aggregate(topic_key)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        grouped = filter_and_group_recent(items)
        payload = {
            "topic": topic_key,
            "count": sum(len(v) for v in grouped.values()),
            "items": sum(grouped.values(), []),
            "recent_grouped": grouped,
        }

    CACHE.set(topic_key, payload)
    return JSONResponse(content=payload)
