"""Event-loop-owned transient photo and MCP labels for the existing overlay."""
import asyncio
import copy


class UiFeedback:
    def __init__(self, on_change, loop=None):
        self._on_change = on_change
        self._loop = loop
        self._tools = {}
        self._photo = None
        self._photo_owner = None
        self._photo_token = None
        self._photo_timer = None
        self._imu = None
        self._imu_owner = None
        self._imu_token = None
        self._imu_timer = None
        self._imu_expanded = False

    def _schedule(self, callback, *args, seconds=3.0):
        loop = self._loop or asyncio.get_running_loop()
        return loop.call_later(seconds, callback, *args)

    @property
    def photo_jpeg(self):
        return self._photo

    @property
    def imu_sample(self):
        return self._imu

    @property
    def imu_expanded(self):
        return self._imu_expanded

    def is_imu_active(self, owner):
        return self._imu is not None and self._imu_owner is owner

    @property
    def tool_name(self):
        if not self._tools:
            return ''
        return self._tools[next(reversed(self._tools))]['name']

    def begin_tool(self, owner, name):
        token = object()
        self._tools[token] = {'owner': owner, 'name': name, 'timer': None}
        self._on_change()
        return token

    def finish_tool(self, token):
        entry = self._tools.get(token)
        if entry is not None and entry['timer'] is None:
            entry['timer'] = self._schedule(self._forget_tool, token)

    def _forget_tool(self, token):
        if self._tools.pop(token, None) is not None:
            self._on_change()

    def show_photo(self, owner, jpeg):
        if self._photo_timer is not None:
            self._photo_timer.cancel()
        token = object()
        self._photo, self._photo_owner, self._photo_token = jpeg, owner, token
        # Start the three seconds after the first frame has been submitted.
        self._on_change()
        self._photo_timer = self._schedule(self._forget_photo, token)

    def _forget_photo(self, token):
        if token is self._photo_token:
            self._photo = self._photo_owner = self._photo_token = None
            self._photo_timer = None
            self._on_change()

    def show_imu(self, owner, sample):
        if self._imu_timer is not None:
            self._imu_timer.cancel()
        token = object()
        self._imu, self._imu_owner, self._imu_token = copy.deepcopy(sample), owner, token
        self._imu_expanded = True
        self._on_change()
        self._imu_timer = self._schedule(self._forget_imu, token)

    def update_imu(self, owner, sample):
        if not self.is_imu_active(owner):
            return False
        self._imu = copy.deepcopy(sample)
        self._on_change()
        return True

    def _forget_imu(self, token):
        if token is self._imu_token:
            self._imu_expanded = False
            self._imu_timer = None
            self._on_change()

    def clear_imu(self):
        if self._imu is None:
            return
        if self._imu_timer is not None:
            self._imu_timer.cancel()
        self._imu = self._imu_owner = self._imu_token = None
        self._imu_expanded = False
        self._imu_timer = None
        self._on_change()

    def clear_owner(self, owner):
        changed = False
        for token, entry in list(self._tools.items()):
            if entry['owner'] is owner:
                if entry['timer'] is not None:
                    entry['timer'].cancel()
                del self._tools[token]
                changed = True
        if self._photo_owner is owner:
            if self._photo_timer is not None:
                self._photo_timer.cancel()
            self._photo = self._photo_owner = self._photo_token = None
            self._photo_timer = None
            changed = True
        if self._imu_owner is owner:
            if self._imu_timer is not None:
                self._imu_timer.cancel()
            self._imu = self._imu_owner = self._imu_token = None
            self._imu_expanded = False
            self._imu_timer = None
            changed = True
        if changed:
            self._on_change()

    def clear(self):
        owners = [entry['owner'] for entry in self._tools.values()]
        if self._photo_owner is not None:
            owners.append(self._photo_owner)
        if self._imu_owner is not None:
            owners.append(self._imu_owner)
        for owner in owners:
            self.clear_owner(owner)
