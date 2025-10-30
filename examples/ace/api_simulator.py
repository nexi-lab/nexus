"""Simulated API for testing ACE learning capabilities.

This module provides a mock API that simulates real-world failures
(rate limits, timeouts, server errors) with configurable failure rates.
"""

import json
import random
import time
from pathlib import Path
from typing import Any


class SimulatedAPIResponse:
    """Simulated HTTP response."""

    def __init__(self, status_code: int, message: str, data: dict | None = None):
        self.status_code = status_code
        self.message = message
        self.data = data or {}
        self.elapsed_ms = 0

    @property
    def ok(self) -> bool:
        """Check if response is successful (2xx status)."""
        return 200 <= self.status_code < 300

    def json(self) -> dict:
        """Get response data as JSON."""
        return self.data


class SimulatedAPI:
    """Simulated API with configurable failure rates.

    Mimics real-world API behavior including:
    - Rate limiting (429)
    - Timeouts (504)
    - Server errors (500, 503)
    - Successful responses (200)
    """

    def __init__(self, config_path: str | None = None):
        """Initialize simulated API.

        Args:
            config_path: Path to API configuration JSON
        """
        if config_path is None:
            config_path = str(Path(__file__).parent / "test_data" / "api_endpoints.json")

        with open(config_path) as f:
            self.config = json.load(f)

        self.endpoints = {ep["url"]: ep for ep in self.config["endpoints"]}
        self.call_count = {}  # Track calls per endpoint
        self.success_count = {}  # Track successes per endpoint

    def get(self, url: str, _timeout: float | None = None) -> SimulatedAPIResponse:
        """Simulate GET request to API endpoint.

        Args:
            url: API endpoint URL
            _timeout: Request timeout (not used in simulation)

        Returns:
            SimulatedAPIResponse with status code and data
        """
        if url not in self.endpoints:
            return SimulatedAPIResponse(404, "Not Found")

        endpoint = self.endpoints[url]

        # Track call
        self.call_count[url] = self.call_count.get(url, 0) + 1

        # Simulate response time
        response_time = endpoint["response_time_ms"] / 1000.0
        time.sleep(response_time)

        # Determine if this call fails
        if random.random() < endpoint["failure_rate"]:
            # Select failure type based on probabilities
            failure_types = endpoint["failure_types"]
            rand = random.random()
            cumulative_prob = 0.0

            for failure in failure_types:
                cumulative_prob += failure["probability"]
                if rand <= cumulative_prob:
                    response = SimulatedAPIResponse(failure["status_code"], failure["message"])
                    response.elapsed_ms = endpoint["response_time_ms"] * 1.5
                    return response

        # Success response
        self.success_count[url] = self.success_count.get(url, 0) + 1

        response = SimulatedAPIResponse(
            200,
            "OK",
            {"endpoint": url, "timestamp": time.time(), "data": f"Success response from {url}"},
        )
        response.elapsed_ms = endpoint["response_time_ms"]
        return response

    def get_stats(self, url: str | None = None) -> dict[str, Any]:
        """Get API call statistics.

        Args:
            url: Optional URL to get stats for specific endpoint

        Returns:
            Dictionary with call counts and success rates
        """
        if url:
            calls = self.call_count.get(url, 0)
            successes = self.success_count.get(url, 0)
            return {
                "url": url,
                "calls": calls,
                "successes": successes,
                "success_rate": successes / calls if calls > 0 else 0.0,
            }

        # Overall stats
        total_calls = sum(self.call_count.values())
        total_successes = sum(self.success_count.values())

        return {
            "total_calls": total_calls,
            "total_successes": total_successes,
            "overall_success_rate": total_successes / total_calls if total_calls > 0 else 0.0,
            "endpoints": {url: self.get_stats(url) for url in self.endpoints},
        }

    def reset_stats(self):
        """Reset all statistics counters."""
        self.call_count.clear()
        self.success_count.clear()


# Example usage for testing
if __name__ == "__main__":
    print("Testing Simulated API...")

    api = SimulatedAPI()

    # Test without retries (baseline)
    print("\n=== Baseline (No Retries) ===")
    for i in range(10):
        response = api.get("https://flaky-api.example.com/users")
        status = "✓" if response.ok else "✗"
        print(f"{status} Call {i + 1}: {response.status_code} - {response.message}")

    stats = api.get_stats("https://flaky-api.example.com/users")
    print(f"\nSuccess Rate: {stats['success_rate']:.0%} ({stats['successes']}/{stats['calls']})")

    # Test with simple retry logic
    print("\n=== With Basic Retries (3 attempts) ===")
    api.reset_stats()

    for i in range(10):
        success = False
        for attempt in range(3):
            response = api.get("https://flaky-api.example.com/users")
            if response.ok:
                success = True
                print(f"✓ Call {i + 1}: Success on attempt {attempt + 1}")
                break
            elif attempt < 2:
                time.sleep(0.1)  # Basic retry delay

        if not success:
            print(f"✗ Call {i + 1}: Failed after 3 attempts")

    stats = api.get_stats("https://flaky-api.example.com/users")
    print(f"\nSuccess Rate: {stats['success_rate']:.0%} ({stats['successes']}/{stats['calls']})")
