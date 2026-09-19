"""unofficial_sixfonia_analytics 共通パッケージ。

重い依存（nagisa, wordcloud, google-cloud-storage 等）はモジュール単位で
遅延importするため、ここでは config のみ公開する。
"""

__version__ = "0.1.0"

from . import config  # noqa: F401
