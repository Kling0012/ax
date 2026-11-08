"""Discord forwarding; loaded by the owning graph through a file-based package."""
from .engine import DiscordForwarder, Post, Target
from .source import UserPostsSource
from .webhook import DiscordWebhook
from .config import load_targets

__all__ = ["DiscordForwarder", "Post", "Target", "UserPostsSource", "DiscordWebhook", "load_targets"]
