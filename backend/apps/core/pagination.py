from rest_framework.pagination import CursorPagination
from rest_framework.response import Response


class DefaultCursorPagination(CursorPagination):
    page_size = 50
    page_size_query_param = "limit"
    max_page_size = 200
    ordering = "-created_at"

    def get_paginated_response(self, data):
        return Response({"items": data, "next_cursor": self.get_next_link(), "previous_cursor": self.get_previous_link()})


class PlainListPagination(CursorPagination):
    """For small lists ordered by another field: override `ordering` in subclasses."""

    page_size = 100
    max_page_size = 500
