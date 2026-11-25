"""Search use cases. SQL owns menu facts; the search port owns its projection."""


class SearchUnavailable(Exception):
    pass


class SearchService:
    def __init__(self, source, index):
        self.source, self.index = source, index

    def search(self, query, category_id, min_price, max_price, offset, limit):
        return self.index.search(self.source.items(), query, category_id, min_price, max_price, offset, limit)

    def suggestions(self, query, limit):
        return self.index.suggestions(self.source.items(), query, limit)
