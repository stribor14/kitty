#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPL v3 Copyright: 2016, Kovid Goyal <kovid at kovidgoyal.net>

import os
import io
import select
import signal
import struct
from threading import Thread
from queue import Queue, Empty

import glfw
from pyte.streams import Stream, DebugStream

from .char_grid import CharGrid
from .screen import Screen
from .tracker import ChangeTracker
from .utils import resize_pty, create_pty


def handle_unix_signals():
    read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda x, y: None)
        signal.siginterrupt(sig, False)
    signal.set_wakeup_fd(write_fd)
    return read_fd


class Boss(Thread):

    daemon = True
    shutting_down = False
    pending_title_change = pending_icon_change = None
    pending_color_changes = {}

    def __init__(self, window, window_width, window_height, opts, args):
        Thread.__init__(self, name='ChildMonitor')
        self.window, self.opts = window, opts
        self.action_queue = Queue()
        self.read_wakeup_fd, self.write_wakeup_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        self.tracker = ChangeTracker(self.mark_dirtied)
        self.screen = Screen(self.opts, self.tracker, self)
        self.char_grid = CharGrid(self.screen, opts, window_width, window_height)
        sclass = DebugStream if args.dump_commands else Stream
        self.stream = sclass(self.screen)
        self.write_buf = memoryview(b'')
        self.child_fd = create_pty()[0]
        self.signal_fd = handle_unix_signals()
        self.readers = [self.child_fd, self.signal_fd, self.read_wakeup_fd]
        self.writers = [self.child_fd]
        resize_pty(80, 24)

    def on_window_resize(self, window, w, h):
        self.queue_action(self.resize_screen, w, h)

    def resize_screen(self, w, h):
        self.char_grid.resize_screen(w, h)

    def apply_opts(self, opts):
        self.opts = opts
        self.queue_action(self.apply_opts_to_screen)

    def apply_opts_to_screen(self):
        self.screen.apply_opts(self.opts)
        self.char_grid.apply_opts(self.opts)
        self.char_grid.dirty_everything()

    def queue_action(self, func, *args):
        self.action_queue.put((func, args))
        self.wakeup()

    def render(self):
        if self.pending_title_change is not None:
            glfw.glfwSetWindowTitle(self.window, self.pending_title_change)
            self.pending_title_change = None
        if self.pending_icon_change is not None:
            self.pending_icon_change = None  # TODO: Implement this
        self.char_grid.render()

    def wakeup(self):
        os.write(self.write_wakeup_fd, b'1')

    def on_wakeup(self):
        try:
            os.read(self.read_wakeup_fd, 1024)
        except (EnvironmentError, BlockingIOError):
            pass
        while not self.shutting_down:
            try:
                func, args = self.action_queue.get_nowait()
            except Empty:
                break
            func(*args)

    def run(self):
        while not self.shutting_down:
            readers, writers, _ = select.select(self.readers, self.writers if self.write_buf else [], [])
            for r in readers:
                if r is self.child_fd:
                    self.read_ready()
                elif r is self.read_wakeup_fd:
                    self.on_wakeup()
                elif r is self.signal_fd:
                    self.signal_received()
            if writers:
                self.write_ready()

    def signal_received(self):
        try:
            data = os.read(self.signal_fd, 1024)
        except BlockingIOError:
            return
        if data:
            signals = struct.unpack('%uB' % len(data), data)
            if signal.SIGINT in signals or signal.SIGTERM in signals:
                self.shutdown()

    def shutdown(self):
        self.shutting_down = True
        glfw.glfwSetWindowShouldClose(self.window, True)
        glfw.glfwPostEmptyEvent()

    def read_ready(self):
        if self.shutting_down:
            return
        try:
            data = os.read(self.child_fd, io.DEFAULT_BUFFER_SIZE)
        except BlockingIOError:
            return
        except EnvironmentError:
            data = b''
        if data:
            self.stream.feed(data)
        else:  # EOF
            self.shutdown()

    def write_ready(self):
        if not self.shutting_down:
            while self.write_buf:
                n = os.write(self.child_fd, self.write_buf)
                if not n:
                    return
                self.write_buf = self.write_buf[n:]

    def write_to_child(self, data):
        if data:
            self.queue_action(self.queue_write, data)

    def queue_write(self, data):
        self.write_buf = memoryview(self.write_buf.tobytes() + data)

    def mark_dirtied(self):
        self.queue_action(self.update_screen)

    def update_screen(self):
        changes = self.tracker.consolidate_changes()
        self.char_grid.update_screen(changes)
        glfw.glfwPostEmptyEvent()

    def title_changed(self, new_title):
        self.pending_title_change = new_title
        glfw.glfwPostEmptyEvent()

    def icon_changed(self, new_icon):
        self.pending_icon_change = new_icon
        glfw.glfwPostEmptyEvent()

    def change_default_color(self, which, value):
        self.pending_color_changes[which] = value
        self.queue_action(self.change_colors)

    def change_colors(self):
        self.char_grid.change_colors(self.pending_color_changes)
        self.pending_color_changes = {}
        glfw.glfwPostEmptyEvent()
