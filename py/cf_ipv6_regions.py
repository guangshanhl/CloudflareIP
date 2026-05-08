import ipaddress
import socket
import sys
import time
import threading
from queue import Queue
from datetime import datetime


TEST_TIMEOUT = 3
TEST_PORT = 443
MAX_THREADS = 8
TOP_NODES = 20

CLOUDFLARE_IPV6_RANGES = [
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]

REGIONS = {
    "JPv6": {
        "output": "JPv6.txt",
        "tag": "jpv6",
        "label": "【东京IPv6】 JP",
        "seed": 11,
    },
    "USv6": {
        "output": "USv6.txt",
        "tag": "usv6",
        "label": "【美国IPv6】 US",
        "seed": 37,
    },
    "SGv6": {
        "output": "SGv6.txt",
        "tag": "sgv6",
        "label": "【新加坡IPv6】 SG",
        "seed": 73,
    },
}


def format_line(ip, config):
    return f"{ip}#{config['tag']} {config['label']}"


def candidate_addresses(seed, per_range=12):
    addresses = []
    for range_index, cidr in enumerate(CLOUDFLARE_IPV6_RANGES):
        network = ipaddress.ip_network(cidr)
        step = 0x100000000 + seed + range_index * 0x10000
        for i in range(1, per_range + 1):
            offset = step * i
            if offset >= network.num_addresses:
                offset = seed + range_index * 257 + i
            addresses.append(str(network.network_address + offset))
    return addresses


class CloudflareIPv6Tester:
    def __init__(self, config):
        self.config = config
        self.nodes = set(candidate_addresses(config["seed"]))
        self.results = []
        self.lock = threading.Lock()

    def test_node_speed(self, ip):
        try:
            start_time = time.time()
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                s.settimeout(TEST_TIMEOUT)
                result = s.connect_ex((ip, TEST_PORT, 0, 0))
                if result == 0:
                    response_time = (time.time() - start_time) * 1000
                    return {
                        "ip": ip,
                        "reachable": True,
                        "response_time_ms": int(response_time),
                        "timestamp": datetime.now().isoformat(),
                    }
                return {
                    "ip": ip,
                    "reachable": False,
                    "response_time_ms": None,
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as exc:
            return {
                "ip": ip,
                "reachable": False,
                "response_time_ms": None,
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def worker(self, queue):
        while not queue.empty():
            ip = queue.get()
            try:
                result = self.test_node_speed(ip)
                with self.lock:
                    self.results.append(result)
            finally:
                queue.task_done()

    def test_all_nodes(self):
        queue = Queue()
        for ip in self.nodes:
            queue.put(ip)

        threads = []
        for _ in range(min(MAX_THREADS, len(self.nodes))):
            thread = threading.Thread(target=self.worker, args=(queue,))
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

    def sorted_results(self):
        reachable_nodes = [
            node for node in self.results
            if node["reachable"] and node["response_time_ms"] is not None
        ]
        return sorted(reachable_nodes, key=lambda node: node["response_time_ms"])

    def output_results(self, results):
        if results:
            nodes = [node["ip"] for node in results[:TOP_NODES]]
        else:
            print(
                "No reachable IPv6 node found; writing candidate list.",
                file=sys.stderr,
            )
            nodes = sorted(self.nodes)[:TOP_NODES]

        for ip in nodes:
            print(format_line(ip, self.config))

    def run(self):
        self.test_all_nodes()
        self.output_results(self.sorted_results())


def run_region(region_key):
    if region_key not in REGIONS:
        raise ValueError(f"Unknown IPv6 region: {region_key}")
    CloudflareIPv6Tester(REGIONS[region_key]).run()


def run_all_regions(region_keys=None):
    keys = region_keys or ("JPv6", "USv6", "SGv6")
    for key in keys:
        if key not in REGIONS:
            raise ValueError(f"Unknown IPv6 region: {key}")
        CloudflareIPv6Tester(REGIONS[key]).run()
