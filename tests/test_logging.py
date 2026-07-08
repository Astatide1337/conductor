"""Tests for structured logging."""

import json
import logging

from conductor.logging import (
    JsonFormatter,
    TextFormatter,
    bind_request_context,
    clear_request_context,
    setup_logging,
    SENSITIVE_HEADERS,
)


class TestJsonFormatter:
    def test_basic_format(self):
        formatter = JsonFormatter()
        record = logging.LogRecord("conductor", logging.INFO, "", 0, "test message", None, None)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["msg"] == "test message"
        assert parsed["lvl"] == "INFO"
        assert "ts" in parsed

    def test_with_context(self):
        bind_request_context("req-1", auth_subject="alice", objective_id="obj-1")
        formatter = JsonFormatter()
        record = logging.LogRecord("conductor", logging.INFO, "", 0, "ctx msg", None, None)
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["req"] == "req-1"
        assert parsed["sub"] == "alice"
        assert parsed["obj"] == "obj-1"
        clear_request_context()

    def test_with_exception(self):
        formatter = JsonFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            record = logging.LogRecord("conductor", logging.ERROR, "", 0, "error msg", None, (type, ValueError("boom"), None))
            output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["lvl"] == "ERROR"
        assert parsed["msg"] == "error msg"
        assert "err" in parsed


class TestTextFormatter:
    def test_basic_format(self):
        formatter = TextFormatter()
        record = logging.LogRecord("conductor", logging.INFO, "", 0, "hello", None, None)
        output = formatter.format(record)
        assert "hello" in output
        assert "INFO" in output


class TestSetupLogging:
    def test_json_setup(self):
        setup_logging(level="INFO", fmt="json")
        logger = logging.getLogger("conductor")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        assert isinstance(logger.handlers[0].formatter, JsonFormatter)

    def test_text_setup(self):
        setup_logging(level="DEBUG", fmt="text")
        logger = logging.getLogger("conductor")
        assert logger.level == logging.DEBUG
        assert isinstance(logger.handlers[0].formatter, TextFormatter)


class TestContextIsolation:
    def test_bind_and_clear(self):
        bind_request_context("r1", "u1", "o1")
        from conductor.logging import request_id_var, auth_subject_var, objective_id_var

        assert request_id_var.get() == "r1"
        assert auth_subject_var.get() == "u1"
        assert objective_id_var.get() == "o1"
        clear_request_context()
        assert request_id_var.get() is None
        assert auth_subject_var.get() is None
        assert objective_id_var.get() is None


class TestSensitiveHeaders:
    def test_known_sensitive_in_set(self):
        assert "authorization" in SENSITIVE_HEADERS
        assert "cookie" in SENSITIVE_HEADERS
        assert "cf-access-jwt-assertion" in SENSITIVE_HEADERS
        assert "x-auth-internal-token" in SENSITIVE_HEADERS

    def test_common_headers_not_sensitive(self):
        assert "content-type" not in SENSITIVE_HEADERS
        assert "accept" not in SENSITIVE_HEADERS