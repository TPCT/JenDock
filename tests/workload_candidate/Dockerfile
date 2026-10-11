FROM sha256:bd9c8acfc81b73714965f4981560a9aab8b43309f903b7da586ef5661ae83c13
WORKDIR /app
COPY --chown=65534:65534 . /app
ENV APP_SERVICES_CACHE=/tmp/laravel-services.php APP_PACKAGES_CACHE=/tmp/laravel-packages.php APP_CONFIG_CACHE=/tmp/laravel-config.php APP_ROUTES_CACHE=/tmp/laravel-routes.php APP_EVENTS_CACHE=/tmp/laravel-events.php
USER 65534:65534
ENTRYPOINT ["php"]
CMD ["artisan", "--version"]
