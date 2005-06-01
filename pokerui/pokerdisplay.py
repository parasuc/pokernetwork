#
# Copyright (C) 2004 Mekensleep
#
# Mekensleep
# 24 rue vieille du temple
# 75004 Paris
#       licensing@mekensleep.com
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#  Loic Dachary <loic@gnu.org>
#
#

class PokerDisplay:
    def __init__(self, *args, **kwargs):
        self.config = kwargs['config']
        self.settings = kwargs['settings']
        self.factory = kwargs['factory']
        self.protocol = None
        self.renderer = None
        self.animations = None
        self.finished = False

    def setProtocol(self, protocol):
        self.protocol = protocol
        if self.animations: self.animations.setProtocol(protocol)

    def unsetProtocol(self):
        if self.animations: self.animations.unsetProtocol()
        self.protocol = None

    def setRenderer(self, renderer):
        self.renderer = renderer

    def finish(self):
        if self.finish:
            return False
        else:
            self.finish = True
            return True
