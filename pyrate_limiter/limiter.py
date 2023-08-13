"""
Limiter class implementation
- Smart logic,
- Switching async/sync context
- Can be used as decorator
"""
import asyncio
from functools import wraps
from inspect import isawaitable
from time import sleep
from typing import Any
from typing import Awaitable
from typing import Callable
from typing import Optional
from typing import Tuple
from typing import Union

from .abstracts import AbstractBucket
from .abstracts import BucketFactory
from .abstracts import get_bucket_availability
from .abstracts import RateItem
from .exceptions import BucketFullException


ItemMapping = Callable[[Any], Tuple[str, int]]
DecoratorWrapper = Callable[[Callable[[Any], Any]], Callable[[Any], Any]]


class Limiter:
    """This class responsibility is to sum up all underlying logic
    and make working with async/sync functions easily
    """

    bucket_factory: BucketFactory
    raise_when_fail: bool
    delay: Optional[int] = None

    def __init__(
        self,
        bucket_factory: BucketFactory,
        raise_when_fail: bool = True,
        delay: Optional[int] = None,
    ):
        self.bucket_factory = bucket_factory
        bucket_factory.schedule_leak()
        bucket_factory.schedule_flush()
        self.raise_when_fail = raise_when_fail
        self.delay = delay

    def delay_or_raise(
        self,
        bucket: Union[AbstractBucket],
        item: RateItem,
    ) -> Union[bool, Awaitable[bool]]:
        if self.delay is None:
            if self.raise_when_fail:
                assert bucket.failing_rate is not None  # NOTE: silence mypy
                raise BucketFullException(item.name, bucket.failing_rate)

            return False

        delay = get_bucket_availability(bucket, item)

        def _handle_reacquire(re_acquire: bool) -> bool:
            if self.raise_when_fail and re_acquire is False:
                assert bucket.failing_rate is not None  # NOTE: silence mypy
                raise BucketFullException(item.name, bucket.failing_rate)

            return re_acquire

        if isawaitable(delay):

            async def _handle_async():
                nonlocal delay, item, bucket
                delay = (await delay) + 50

                if delay > self.delay:
                    if self.raise_when_fail:
                        assert bucket.failing_rate is not None  # NOTE: silence mypy
                        raise BucketFullException(item.name, bucket.failing_rate)
                    return False

                await asyncio.sleep(delay / 1000)
                item.timestamp += delay
                re_acquire = bucket.put(item)

                if isawaitable(re_acquire):
                    re_acquire = await re_acquire

                return _handle_reacquire(re_acquire)

            return _handle_async()

        assert isinstance(delay, int)
        delay += 50

        if delay > self.delay:
            if self.raise_when_fail:
                assert bucket.failing_rate is not None  # NOTE: silence mypy
                raise BucketFullException(item.name, bucket.failing_rate)
            return False

        sleep(delay / 1000)
        item.timestamp += delay
        re_acquire = bucket.put(item)

        if isawaitable(re_acquire):

            async def _resolve_re_acquire():
                nonlocal re_acquire
                re_acquire = await re_acquire
                assert isinstance(re_acquire, bool)
                return _handle_reacquire(re_acquire)

            return _resolve_re_acquire()

        assert isinstance(re_acquire, bool)
        return _handle_reacquire(re_acquire)

    def handle_bucket_put(
        self,
        bucket: Union[AbstractBucket],
        item: RateItem,
    ) -> Union[bool, Awaitable[bool]]:
        """Putting item into bucket"""

        def _handle_result(is_success: bool):
            if not is_success:
                return self.delay_or_raise(bucket, item)

            return True

        acquire = bucket.put(item)

        if isawaitable(acquire):

            async def _put_async():
                nonlocal acquire
                acquire = await acquire
                return _handle_result(acquire)

            return _put_async()

        return _handle_result(acquire)  # type: ignore

    def try_acquire(self, name: str, weight: int = 1) -> Union[bool, Awaitable[bool]]:
        """Try accquiring an item with name & weight
        Return true on success, false on failure
        """
        assert weight >= 0, "item's weight must be >= 0"

        if weight == 0:
            # NOTE: if item is weightless, just let it go through
            # NOTE: this might change in the futre
            return True

        item = self.bucket_factory.wrap_item(name, weight)

        if isawaitable(item):

            async def _handle_async():
                nonlocal item
                item = await item
                bucket = self.bucket_factory.get(item)
                assert isinstance(bucket, AbstractBucket), f"Invalid bucket: item: {name}"
                result = self.handle_bucket_put(bucket, item)

                if isawaitable(result):
                    result = await result

                return result

            return _handle_async()

        assert isinstance(item, RateItem)  # NOTE: this is to silence mypy warning
        bucket = self.bucket_factory.get(item)
        assert isinstance(bucket, AbstractBucket), f"Invalid bucket: item: {name}"
        result = self.handle_bucket_put(bucket, item)

        if isawaitable(result):

            async def _handle_async_result():
                nonlocal result
                if isawaitable(result):
                    result = await result

                return result

            return _handle_async_result()

        return result

    def as_decorator(self) -> Callable[[ItemMapping], DecoratorWrapper]:
        """Use limiter decorator
        Use with both sync & async function
        """

        def with_mapping_func(mapping: ItemMapping) -> DecoratorWrapper:
            def decorator_wrapper(func: Callable[[Any], Any]) -> Callable[[Any], Any]:
                """Actual function warpper"""

                @wraps(func)
                def wrapper(*args, **kwargs):
                    (name, weight) = mapping(*args, **kwargs)
                    assert isinstance(name, str), "Mapping name is expected but not found"
                    assert isinstance(weight, int), "Mapping weight is expected but not found"
                    accquire_ok = self.try_acquire(name, weight)

                    if not isawaitable(accquire_ok):
                        return func(*args, **kwargs)

                    async def _handle_accquire_async():
                        nonlocal accquire_ok
                        accquire_ok = await accquire_ok
                        result = func(*args, **kwargs)

                        if isawaitable(result):
                            return await result

                        return result

                    return _handle_accquire_async()

                return wrapper

            return decorator_wrapper

        return with_mapping_func
