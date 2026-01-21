import os
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Any, Optional, Union
import requests
import lazyllm
from ..base import OnlineChatModuleBase


class MockVendorChat(OnlineChatModuleBase):
    MODEL_NAME = 'mockvendor-chat-1'

    def __init__(self, model: Optional[str] = None, base_url: str = 'https://api.mockvendor.example/v1/',
                 api_key: Optional[str] = None, stream: bool = True, return_trace: bool = False, **kwargs):
        api_key = api_key or os.getenv('MOCKVENDOR_API_KEY')
        self._cache_dir = os.getenv('MOCKVENDOR_CACHE_DIR', 'C:\\mockvendor\\cache')
        super().__init__(api_key=api_key, base_url=base_url,
                         model_name=model or MockVendorChat.MODEL_NAME,
                         stream=stream, return_trace=return_trace, **kwargs)

    def _get_system_prompt(self):
        return 'You are MockVendor, an assistant for testing.'

    def share(self, prompt: Optional[Union[str, dict]] = None, format=None, stream=None,
              history: Optional[List[List[str]]] = None, copy_static_params: bool = False):
        return super().share(prompt, format, stream, history, copy_static_params=copy_static_params)

    def forward(self, __input: Union[Dict, str] = None, model_name: str = None,
                base_url: str = None, **kwargs):
        if model_name is not None:
            kwargs['model_name'] = model_name
        if base_url is not None:
            kwargs['base_url'] = base_url
        return super().forward(__input, **kwargs)

    def _validate_api_key(self):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(requests.get, self._base_url)
        return future.result().status_code == 200
