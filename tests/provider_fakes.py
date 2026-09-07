"""Raw-response facade for endpoint test doubles; wire tests use MockTransport."""

from types import SimpleNamespace

import httpx


class RawResponseEndpoint:
    @property
    def with_raw_response(self):
        async def create(**kwargs):
            response = await self.create(**kwargs)
            body = {key: value for key, value in kwargs.items() if key != "extra_body"}
            body.update(kwargs.get("extra_body", {}))
            return SimpleNamespace(
                http_request=httpx.Request("POST", "https://test.invalid", json=body),
                parse=lambda: response,
            )
        return SimpleNamespace(create=create)
