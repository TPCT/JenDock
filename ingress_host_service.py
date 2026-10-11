"""Trusted in-process host capability assembly, not a daemon or request factory.

The administrator supplies one adapter and its issuer. The issuer owns its
SQLite connection and must be invoked on that connection's owning thread.
"""


class HostService:
    def __init__(self, adapter, issuer, *, available_ports=None, tls=None):
        if issuer.adapter is not adapter:
            raise ValueError('Host issuer and service must share the same adapter')
        callback = issuer.authorize_route
        if not callable(callback) or not callable(issuer.issue):
            raise ValueError('Host issuer requires authorization and issuance capabilities')
        self._authorization_callback = callback
        if available_ports is not None and not callable(available_ports):
            raise ValueError("Host port provider must be callable")
        self._available_ports = available_ports
        self.adapter = adapter
        self.issuer = issuer
        self.adapter.authorize_route = callback
        self.tls = tls
        self._tls_capability = tls
        self._certificate_manager = getattr(tls, "certificates", None)
        self._related_configs_callback = getattr(adapter, "related_configs", None)
        self._certificate_callback = None
        if tls is not None:
            if tls.http is not adapter or not callable(issuer.authorize_certificate):
                raise ValueError('TLS capability must share the authorized HTTP adapter')
            self._certificate_callback = issuer.authorize_certificate
            tls.authorize_certificate = self._certificate_callback


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

    def available_ports(self, request):
        self._check_authorization()
        if not isinstance(request, dict) or request:
            raise ValueError('Host port discovery accepts no arguments')
        if self._available_ports is None:
            raise ValueError('Host port observation is not configured')
        return self._available_ports()

    def capabilities(self, request):
        self._check_authorization()
        if not isinstance(request, dict) or request:
            raise ValueError('Host capability discovery accepts no arguments')
        if self.tls is not None: self._tls()
        return {'schema': 1, 'http': True, 'tls': self.tls is not None}

    def _tls(self):
        self._check_authorization()
        if (self.tls is None or self.tls is not self._tls_capability
                or self.tls.http is not self.adapter
                or getattr(self.tls, 'certificates', None) is not self._certificate_manager
                or self.adapter.related_configs is not self._related_configs_callback
                or self.tls.authorize_certificate is not self._certificate_callback):
            raise ValueError('TLS capability is not configured or changed')
        return self.tls

    def request_certificate(self, request): return self._tls().request_certificate(request)
    def stage_tls(self, request): return self._tls().stage_tls(request)
    def activate_tls(self, request): return self._tls().activate_tls(request)
    def inspect_tls(self, request): return self._tls().inspect_tls(request)
    def verify_tls(self, request): return self._tls().verify_tls(request)
    def recover_tls(self, request): return self._tls().recover_tls(request)
