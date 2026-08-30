"""
东财数据限流防封机制
==================

根据a-stock-data SKILL.md的要求，所有东财接口都需要通过em_get()进行限流。

核心原则：
1. 串行，不并发 — 绝不对东财开多线程
2. 每次间隔 ≥ 1秒 + 随机抖动
3. 复用HTTP会话（Keep-Alive）
4. 带正常UA + Referer
5. 批量场景每只股票之间sleep
"""

import time
import random
import requests
from typing import Dict, Any, Optional


# 东财限流配置
EM_MIN_INTERVAL = 1.0  # 最小请求间隔（秒）
EM_MAX_JITTER = 0.5    # 随机抖动最大值（秒）
EM_BATCH_INTERVAL = 1.5  # 批量请求间隔（秒）
EM_MAX_RETRIES = 3     # 最大重试次数


class EastMoneyRateLimiter:
    """东财数据限流器"""

    def __init__(self):
        self.session = requests.Session()
        self.last_request_time = 0
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Referer': 'https://quote.eastmoney.com'
        })

    def _wait_if_needed(self, is_batch: bool = False):
        """等待如果距离上次请求时间太短"""
        current_time = time.time()
        elapsed = current_time - self.last_request_time

        # 根据请求类型确定最小间隔
        min_interval = EM_BATCH_INTERVAL if is_batch else EM_MIN_INTERVAL

        if elapsed < min_interval:
            wait_time = min_interval - elapsed + random.uniform(0, EM_MAX_JITTER)
            time.sleep(wait_time)

        self.last_request_time = time.time()

    def _handle_rate_limit(self, response: requests.Response) -> int:
        """H08: 处理429限流，读取Retry-After头。"""
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                wait_time = int(retry_after)
            except ValueError:
                wait_time = random.uniform(5, 10)
        else:
            wait_time = random.uniform(5, 10)
        return wait_time

    def get(self, url: str, params: Optional[Dict[str, Any]] = None,
            is_batch: bool = False, **kwargs) -> Optional[requests.Response]:
        """带限流的GET请求"""
        for attempt in range(EM_MAX_RETRIES):
            try:
                self._wait_if_needed(is_batch)

                response = self.session.get(url, params=params, **kwargs)

                # H08: 检查是否需要限流，读取Retry-After头
                if response.status_code == 429:
                    wait_time = self._handle_rate_limit(response)
                    time.sleep(wait_time)
                    continue

                return response

            except requests.exceptions.RequestException as e:
                if attempt < EM_MAX_RETRIES - 1:
                    time.sleep(random.uniform(1, 3))
                else:
                    raise e

        return None

    def post(self, url: str, data: Optional[Dict[str, Any]] = None,
             is_batch: bool = False, **kwargs) -> Optional[requests.Response]:
        """H07: 带限流的POST请求"""
        for attempt in range(EM_MAX_RETRIES):
            try:
                self._wait_if_needed(is_batch)

                response = self.session.post(url, data=data, **kwargs)

                # H08: 检查是否需要限流，读取Retry-After头
                if response.status_code == 429:
                    wait_time = self._handle_rate_limit(response)
                    time.sleep(wait_time)
                    continue

                return response

            except requests.exceptions.RequestException as e:
                if attempt < EM_MAX_RETRIES - 1:
                    time.sleep(random.uniform(1, 3))
                else:
                    raise e

        return None

    def close(self):
        """关闭会话"""
        self.session.close()


# 全局限流器实例
rate_limiter = EastMoneyRateLimiter()


def em_get(url: str, params: Optional[Dict[str, Any]] = None,
           is_batch: bool = False, **kwargs) -> Optional[requests.Response]:
    """东财数据统一获取入口（带限流）"""
    return rate_limiter.get(url, params, is_batch, **kwargs)


def em_post(url: str, data: Optional[Dict[str, Any]] = None,
            is_batch: bool = False, **kwargs) -> Optional[requests.Response]:
    """东财数据统一POST入口（带限流）"""
    return rate_limiter.post(url, data, is_batch, **kwargs)
