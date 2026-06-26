"""Group model (e.g. @TTP, @TTPNE).

A group is a named, broadcast-style destination. This app owns the cross-transport
group convention: each transport adapter maps a group to its native mechanism
(native ``@``-groups on JS8Call, a defined destination convention on Reticulum,
labelled broadcast elsewhere).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Group:
    """A named collective and where it is active for outbound sends."""

    name: str                               # without the leading '@'
    display_name: str = ""
    transports: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.name = self.name.lstrip("@")
        if not self.display_name:
            self.display_name = self.name

    @property
    def tag(self) -> str:
        return f"@{self.name}"


class GroupRegistry:
    """In-memory view of configured groups + the user's subscriptions."""

    def __init__(
        self,
        groups: dict[str, Group] | None = None,
        subscriptions: set[str] | None = None,
        show_unsubscribed: bool = False,
    ) -> None:
        self._groups: dict[str, Group] = groups or {}
        self._subscriptions: set[str] = subscriptions or set()
        self.show_unsubscribed = show_unsubscribed

    # -- construction ---------------------------------------------------------

    @classmethod
    def from_config(cls, config: object) -> GroupRegistry:
        from ..config import Config  # local import to avoid a cycle

        assert isinstance(config, Config)
        groups = {
            name: Group(
                name=name,
                display_name=data.get("display_name", name),
                transports=list(data.get("transports", [])),
            )
            for name, data in config.groups.items()
        }
        subs = {g.lstrip("@") for g in config.subscriptions.get("groups", [])}
        show = bool(config.subscriptions.get("show_unsubscribed", False))
        return cls(groups, subs, show)

    # -- queries --------------------------------------------------------------

    def get(self, name: str) -> Group | None:
        return self._groups.get(name.lstrip("@"))

    def all(self) -> list[Group]:
        return list(self._groups.values())

    def is_subscribed(self, name: str) -> bool:
        return name.lstrip("@") in self._subscriptions

    def transports_for(self, name: str) -> list[str]:
        group = self.get(name)
        return list(group.transports) if group else []

    # -- mutation (GUI / CLI driven; persist via Config) ----------------------

    def subscribe(self, name: str) -> None:
        self._subscriptions.add(name.lstrip("@"))

    def unsubscribe(self, name: str) -> None:
        self._subscriptions.discard(name.lstrip("@"))

    @property
    def subscriptions(self) -> set[str]:
        return set(self._subscriptions)

