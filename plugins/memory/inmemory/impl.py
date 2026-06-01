class InMemoryStore:
    def __init__(self):
        self._data = {}

    async def put(self, key, value, ctx):
        self._data[key] = value

    async def search(self, query, limit, ctx):
        items = []
        for k, v in self._data.items():
            txt = str(v)
            if query.lower() in txt.lower():
                items.append({"key": k, "value": v})
        return items[:limit]

    async def summarize(self, ctx):
        return {"count": len(self._data), "keys": sorted(self._data.keys())}
