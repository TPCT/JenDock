"""Trusted in-process host capability assembly, not a daemon or request factory.

The administrator supplies one adapter and its issuer. The issuer owns its
SQLite connection and must be invoked on that connection's owning thread.
"""


class HostService:
    def __init__(self, adapter, issuer):
        if issuer.adapter is not adapter:
            raise ValueError('Host issuer and service must share the same adapter')
        callback = issuer.authorize_route
        if not callable(callback) or not callable(issuer.issue):
            raise ValueError('Host issuer requires authorization and issuance capabilities')
        self._authorization_callback = callback
        self.adapter = adapter
        self.issuer = issuer
        self.adapter.authorize_route = callback

    def _check_authorization(self):
        # Compare the exact installed object, never user-defined equality or a
        # newly retrieved bound method. A replaced hook cannot enable static mode.
        if self.adapter.authorize_route is not self._authorization_callback:
            raise ValueError('Host authorization callback changed')

    def issue_grant(self, request):
        self._check_authorization()
        return self.issuer.issue(request)

    def stage_http(self, request):
        self._check_authorization()
        return self.adapter.stage_http(request)

    def activate_http(self, request):
        self._check_authorization()
        return self.adapter.activate_http(request)

    def inspect(self, request):
        self._check_authorization()
        return self.adapter.inspect(request)

    def verify(self, request):
        self._check_authorization()
        return self.adapter.verify(request)

    def recover(self, request):
        self._check_authorization()
        return self.adapter.recover(request)
