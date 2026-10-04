import logging
import os
import sys
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from opentelemetry import metrics, trace, _logs
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    ConsoleSpanExporter,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import (
    PeriodicExportingMetricReader,
    ConsoleMetricExporter,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    SimpleLogRecordProcessor,
    BatchLogRecordProcessor,
    ConsoleLogRecordExporter,
)
from opentelemetry.instrumentation.logging import LoggingInstrumentor

logger = logging.getLogger("order-tracker")
tracer = trace.get_tracer("order-tracker")
requests_counter = None

_initialized = False
_trace_provider = None
_logger_provider = None
_meter_provider = None


def setup_telemetry(app):
    global _initialized, requests_counter, _trace_provider, _logger_provider, _meter_provider
    if _initialized:
        return
    _initialized = True

    resource = Resource.create({"service.name": "order-tracker"})
    otlp_endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

    if otlp_endpoint:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
            OTLPMetricExporter,
        )
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )

        _trace_provider = TracerProvider(resource=resource)
        _trace_provider.add_span_processor(
            SimpleSpanProcessor(OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True))
        )
        trace.set_tracer_provider(_trace_provider)

        _logger_provider = LoggerProvider(resource=resource)
        _logger_provider.add_log_record_processor(
            SimpleLogRecordProcessor(
                OTLPLogExporter(endpoint=otlp_endpoint, insecure=True)
            )
        )
        _logs.set_logger_provider(_logger_provider)

        metric_reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=otlp_endpoint, insecure=True),
            export_interval_millis=1000,
        )
        _meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(_meter_provider)
    else:
        _trace_provider = TracerProvider(resource=resource)
        _trace_provider.add_span_processor(
            SimpleSpanProcessor(ConsoleSpanExporter())
        )
        trace.set_tracer_provider(_trace_provider)

        _logger_provider = LoggerProvider(resource=resource)
        _logger_provider.add_log_record_processor(
            SimpleLogRecordProcessor(ConsoleLogRecordExporter())
        )
        _logs.set_logger_provider(_logger_provider)

        metric_reader = PeriodicExportingMetricReader(
            ConsoleMetricExporter(), export_interval_millis=1000
        )
        _meter_provider = MeterProvider(
            resource=resource, metric_readers=[metric_reader]
        )
        metrics.set_meter_provider(_meter_provider)

    LoggingInstrumentor().instrument(set_logging_format=True)
    handler = LoggingHandler(level=logging.INFO, logger_provider=_logger_provider)
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    meter = metrics.get_meter("order-tracker")
    requests_counter = meter.create_counter(
        name="http_requests_total",
        description="Total HTTP requests recorded by route and status code",
        unit="1",
    )

    class TelemetryMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            status_code = 500

            with tracer.start_as_current_span(f"{request.method} {path}") as span:
                try:
                    response = await call_next(request)
                    status_code = response.status_code
                    return response
                except Exception as exc:
                    span.record_exception(exc)
                    status_code = 500
                    raise exc
                finally:
                    route = request.scope.get("route")
                    route_path = route.path if route else path

                    span.set_attribute("http.status_code", status_code)
                    span.set_attribute("http.route", route_path)
                    span.set_attribute("http.method", request.method)

                    order_id = request.path_params.get("order_id")
                    if order_id:
                        span.set_attribute("order.id", order_id)

                    if requests_counter is not None:
                        requests_counter.add(
                            1,
                            {
                                "route": route_path,
                                "status_code": status_code,
                                "method": request.method,
                            },
                        )

                    logger.info(
                        f"{request.method} {route_path} completed with status {status_code}",
                        extra={
                            "route": route_path,
                            "status_code": status_code,
                            "order_id": order_id,
                        },
                    )

    app.add_middleware(TelemetryMiddleware)


def shutdown_telemetry():
    global _trace_provider, _logger_provider, _meter_provider
    if _meter_provider:
        try:
            _meter_provider.shutdown()
        except Exception:
            pass
    if _trace_provider:
        try:
            _trace_provider.shutdown()
        except Exception:
            pass
    if _logger_provider:
        try:
            _logger_provider.shutdown()
        except Exception:
            pass
