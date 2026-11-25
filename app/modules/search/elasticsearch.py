"""Small-menu, serialized SQL→ES projection with explicit dirty/failure handling."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import httpx
from app.modules.search.service import SearchUnavailable


class ElasticsearchMenu:
    def __init__(self, url, index, state_dir):
        if not url or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", index):
            raise SearchUnavailable()
        self.url, self.index = url.rstrip("/"), index
        self.state_dir = Path(state_dir)

    @contextmanager
    def locked_client(self, *, nonblocking=False):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.state_dir / (self.index + ".lock")
        with lock.open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
            except BlockingIOError:
                raise SearchUnavailable() from None
            try:
                with httpx.Client(base_url=self.url, timeout=5, trust_env=False) as client:
                    yield client
            except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError):
                raise SearchUnavailable() from None
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    @staticmethod
    def request(client, method, path, **kwargs):
        response = client.request(method, path, **kwargs)
        if response.status_code >= 300:
            raise SearchUnavailable()
        return response.json()

    def synchronize(self, client, items):
        fingerprint = hashlib.sha256(json.dumps({"version": 2, "items": items}, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
        exists = client.head('/' + self.index)
        if exists.status_code == 404:
            self.request(client, "PUT", '/' + self.index, json={"settings": {"number_of_shards": 1, "number_of_replicas": 0},
                "mappings": {"_meta": {"fingerprint": "dirty"}, "properties": {
                    "id": {"type": "integer"}, "name": {"type": "text", "analyzer": "russian"},
                    "description": {"type": "text", "analyzer": "russian"}, "suggest": {"type": "completion"},
                    "category_id": {"type": "integer"}, "category_name": {"type": "keyword"},
                    "price": {"type": "double"}, "is_available": {"type": "boolean"}}}})
        elif exists.status_code != 200:
            raise SearchUnavailable()
        mapping = self.request(client, "GET", '/' + self.index + '/_mapping')
        properties = mapping[self.index]["mappings"].get("properties", {})
        if not {"description", "suggest"}.issubset(properties):
            # Add fields in place; never recreate the approved index/credentials.
            self.request(client, "PUT", '/' + self.index + '/_mapping', json={
                "properties": {"description": {"type": "text", "analyzer": "russian"}, "suggest": {"type": "completion"}},
                "_meta": {"fingerprint": "dirty"}})
            mapping[self.index]["mappings"]["_meta"] = {"fingerprint": "dirty"}
        current = mapping[self.index]["mappings"].get("_meta", {}).get("fingerprint")
        if current == fingerprint:
            return
        self.request(client, "PUT", '/' + self.index + '/_mapping', json={"_meta": {"fingerprint": "dirty"}})
        deletion = self.request(client, "POST", '/' + self.index + '/_delete_by_query', params={"refresh": "true"}, json={"query": {"match_all": {}}})
        # HTTP 200 can still describe a partial/failed deletion. Never commit it clean.
        if (not isinstance(deletion, dict) or deletion.get("timed_out") is not False
                or deletion.get("failures") != []
                or any(type(deletion.get(key)) is not int or deletion[key] < 0 for key in ("total", "deleted", "version_conflicts"))
                or deletion["version_conflicts"] != 0 or deletion["deleted"] != deletion["total"]):
            raise SearchUnavailable()
        if items:
            body = ''.join(json.dumps({"index": {"_index": self.index, "_id": item['id']}}) + '\n' + json.dumps({**item, 'suggest': {'input': list(dict.fromkeys([item['name'], *re.findall(r'[\w]+', item['name'])]))}}, ensure_ascii=False, allow_nan=False) + '\n' for item in items)
            result = self.request(client, "POST", '/_bulk', params={"refresh": "wait_for"}, content=body.encode(), headers={"Content-Type": "application/x-ndjson"})
            if result.get("errors"):
                raise SearchUnavailable()
        self.request(client, "PUT", '/' + self.index + '/_mapping', json={"_meta": {"fingerprint": fingerprint}})

    def search(self, items, query, category_id, min_price, max_price, offset, limit):
        filters = []
        if category_id is not None:
            filters.append({"term": {"category_id": category_id}})
        bounds = {key: value for key, value in (("gte", min_price), ("lte", max_price)) if value is not None}
        if bounds:
            filters.append({"range": {"price": bounds}})
        text_query = {"multi_match": {"query": query, "fields": ["name^2", "description"], "fuzziness": "AUTO"}} if query else {"match_all": {}}
        with self.locked_client() as client:
            self.synchronize(client, items)
            data = self.request(client, "POST", '/' + self.index + '/_search', json={"from": offset, "size": limit,
                "track_total_hits": True, "query": {"bool": {"must": [text_query], "filter": filters}},
                "sort": [{"_score": "desc"}, {"id": "asc"}],
                "_source": ["id", "name", "description", "price", "category_id", "category_name", "is_available"],
                "aggs": {"categories": {"terms": {"field": "category_id", "size": 1000},
                                         "aggs": {"label": {"top_hits": {"size": 1, "_source": ["category_name"]}}}},
                         "price_min": {"min": {"field": "price"}}, "price_max": {"max": {"field": "price"}},
                         "price_ranges": {"range": {"field": "price", "ranges": [
                             {"key": "0..150", "from": 0, "to": 150},
                             {"key": "150..300", "from": 150, "to": 300},
                             {"key": "300+", "from": 300}]}}}})
        return {"items": [hit["_source"] for hit in data["hits"]["hits"]],
                "total": data["hits"]["total"]["value"], "engine": "elasticsearch",
                "facets": {"categories": [{"category_id": b["key"],
                    "name": b["label"]["hits"]["hits"][0]["_source"].get("category_name"), "count": b["doc_count"]}
                    for b in data["aggregations"]["categories"]["buckets"]],
                    "price": {"min": data["aggregations"]["price_min"]["value"],
                              "max": data["aggregations"]["price_max"]["value"],
                              "buckets": [{"key": b["key"], "from": b.get("from", 0), "to": b.get("to"), "count": b["doc_count"]}
                                          for b in data["aggregations"]["price_ranges"]["buckets"]]}}}

    def refresh(self, items, *, nonblocking=False):
        with self.locked_client(nonblocking=nonblocking) as client:
            self.synchronize(client, items)

    def suggestions(self, items, query, limit):
        with self.locked_client() as client:
            self.synchronize(client, items)
            if not query.strip():
                data = self.request(client, "POST", '/' + self.index + '/_search', json={"size": limit,
                    "query": {"match_all": {}}, "sort": [{"id": "asc"}], "_source": ["id", "name"]})
                return {"suggestions": [hit["_source"] for hit in data["hits"]["hits"]]}
            data = self.request(client, "POST", '/' + self.index + '/_search', json={"size": 0,
                "suggest": {"menu": {"prefix": query, "completion": {"field": "suggest", "size": limit, "skip_duplicates": True}}},
                "_source": ["id", "name"]})
        return {"suggestions": [{"id": o["_source"]["id"], "name": o["_source"]["name"]}
                                for o in data["suggest"]["menu"][0]["options"]]}
