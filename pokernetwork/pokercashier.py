#
# -*- py-indent-offset: 4; coding: utf-8; mode: python -*-
#
# Copyright (C) 2006, 2007, 2008, 2009 Loic Dachary <loic@dachary.org>
# Copyright (C) 2008             Bradley M. Kuhn <bkuhn@ebb.org>
# Copyright (C) 2006 Mekensleep <licensing@mekensleep.com>
#                    24 rue vieille du temple, 75004 Paris
#
# This software's license gives you freedom; you can copy, convey,
# propagate, redistribute and/or modify this program under the terms of
# the GNU Affero General Public License (AGPL) as published by the Free
# Software Foundation (FSF), either version 3 of the License, or (at your
# option) any later version of the AGPL published by the FSF.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Affero
# General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program in a file in the toplevel directory called
# "AGPLv3".  If not, see <http://www.gnu.org/licenses/>.
#
# Authors:
#  Loic Dachary <loic@dachary.org>
#

from MySQLdb.constants import ER

from twisted.internet import reactor

from pokernetwork import currencyclient
from pokernetwork import pokerlock
from pokerpackets.packets import *
from pokerpackets.networkpackets import *
from pokernetwork import log as network_log
log = network_log.get_child('pokercashier')

PokerLock = pokerlock.PokerLock

class PokerCashier:

    log = log.get_child('PokerCashier')

    def __init__(self, settings):
        self.settings = settings
        self.currency_client = currencyclient.CurrencyClient()
        self.parameters = settings.headerGetProperties("/server/cashier")[0]
        if 'pokerlock_queue_timeout' in self.parameters:
            PokerLock.queue_timeout = int(self.parameters['pokerlock_queue_timeout'])
        self.db = None
        self.db_parameters = settings.headerGetProperties("/server/database")[0]
        self.locks = {}
        reactor.callLater(0, self.resumeCommits)

    def close(self):
        for lock in self.locks.values():
            lock.close()
        del self.db
        
    def setDb(self, db):
        self.db = db

    def resumeCommits(self):
        pass
    
    def getCurrencySerial(self, url, reentrant = True):
        cursor = self.db.cursor()
        #
        # Figure out the currency_serial matching the URL
        #
        sql = "SELECT serial FROM currencies WHERE url = %s"
        cursor.execute(sql, (url,))
        self.log.debug("%s", cursor._executed)
        if cursor.rowcount == 0:
            user_create = self.parameters.get('user_create', 'no').lower()
            if user_create not in ('yes', 'on'):
                raise PacketError(
                    other_type = PACKET_POKER_CASH_IN,
                    code = PacketPokerCashIn.REFUSED, #TODO better error code
                    message = "Invalid currency %s and user_create = %s in settings." % (url,user_create)
                )
            sql = "INSERT INTO currencies (url) VALUES (%s)"
            try:
                cursor.execute(sql, (url,))
                self.log.debug("%s", cursor._executed)
                if cursor.rowcount == 1:
                    currency_serial = cursor.lastrowid
                else:
                    raise Exception("SQL command '%s' failed without raising exception.  Underlying DB may be severely hosed" % sql)
            except Exception, e:
                cursor.close()
                if e[0] == ER.DUP_ENTRY and reentrant:
                    #
                    # Insertion failed, assume it's because another
                    # process already inserted it.
                    #
                    return self.getCurrencySerial(url, False)
                else:
                    raise
        else:
            (currency_serial,) = cursor.fetchone()

        cursor.close()
        return currency_serial

    def cashGeneralFailure(self, reason, packet):
        self.log.warn("cashGeneralFailure: %s packet = %s", reason, packet)
        from twisted.python import failure
        from twisted.web import error
        if isinstance(reason, failure.Failure) and isinstance(reason.value, error.Error):
            self.log.error("cashGeneralFailure: response = %s", reason.value.response)
                
        if hasattr(packet, "currency_serial"):
            self.unlock(packet.currency_serial)
            del packet.currency_serial
        return reason

    def cashInUpdateSafe(self, result, transaction_id, packet):
        self.log.debug("cashInUpdateSafe: %s", packet)
        cursor = self.db.cursor()
        cursor.execute("START TRANSACTION")
        try:
            sqls = []
            sqls.append( ( "INSERT INTO safe SELECT currency_serial, serial, name, value FROM counter "  +
                           "       WHERE transaction_id = '" + transaction_id + "' AND " +
                           "             status = 'n' ", 1 ) )
            sqls.append( ( "DELETE FROM counter,safe USING counter,safe WHERE " +
                           " counter.currency_serial = safe.currency_serial AND " +
                           " counter.serial = safe.serial AND " +
                           " counter.value = safe.value AND " +
                           " counter.status = 'y' ", 0 ) )
            sqls.append( ( "DELETE FROM counter WHERE transaction_id = '" + transaction_id + "'", 1 ) )
            sqls.append( ( "INSERT INTO user2money (user_serial, currency_serial, amount) VALUES (" +
                           str(packet.serial) + ", " + str(packet.currency_serial) + ", " + str(packet.value) + ") " +
                           " ON DUPLICATE KEY UPDATE amount = amount + " + str(packet.value), 0 ) )

            for ( sql, rowcount ) in sqls:
                if cursor.execute(sql) < rowcount:
                    message = sql + " affected " + str(cursor.rowcount) + " records instead >= " + str(rowcount)
                    self.log.error("%s", message)
                    raise PacketError(
                        other_type = PACKET_POKER_CASH_IN,
                        code = PacketPokerCashIn.SAFE,
                        message = message
                    )
                self.log.debug("cashInUpdateSafe: %d: %s", cursor.rowcount, sql)
            cursor.execute("COMMIT")
            cursor.close()
        except:
            cursor.execute("ROLLBACK")
            cursor.close()
            raise

        self.unlock(packet.currency_serial);
        return PacketAck()

    def cashInUpdateCounter(self, new_notes, packet, old_notes):
        self.log.debug("cashInUpdateCounter: new_notes = %s old_notes = %s", new_notes, old_notes)
        #
        # The currency server gives us new notes to replace the
        # old ones. These new notes are not valid yet, the
        # currency server is waiting for our commit. Store all
        # the notes involved in the transaction on the counter.
        #
        cursor = self.db.cursor()
        cursor.execute("START TRANSACTION")
        transaction_id = new_notes[0][2]
        try:
            def notes_on_counter(notes, transaction_id, status):
                for ( url, serial, name, value ) in notes:
                    sql = ( "INSERT INTO counter ( transaction_id, user_serial, currency_serial, serial, name, value, status, application_data) VALUES " +
                            "                    ( %s,             %s,          %s,              %s,     %s,   %s,    %s,     %s )" )
                    cursor.execute(sql, ( transaction_id, packet.serial, packet.currency_serial, serial, name, value, status, packet.application_data ));
            notes_on_counter(new_notes, transaction_id, 'n')
            notes_on_counter(old_notes, transaction_id, 'y')
            cursor.execute("COMMIT")
            cursor.close()
        except:
            cursor.execute("ROLLBACK")
            cursor.close()
            raise

        return self.cashInCurrencyCommit(transaction_id, packet)

    def cashInCurrencyCommit(self, transaction_id, packet):
        self.log.debug("cashInCurrencyCommit")
        deferred = self.currency_client.commit(packet.url, transaction_id)
        deferred.addCallback(self.cashInUpdateSafe, transaction_id, packet)
        return deferred
        
    def cashInValidateNote(self, lock_name, packet):
        #
        # Ask the currency server for change
        #
        cursor = self.db.cursor()
        try:
            sql = \
                "SELECT transaction_id FROM counter " \
                "WHERE currency_serial = %s " \
                "AND serial = %s"
            params = (packet.currency_serial,packet.bserial)
            cursor.execute(sql,params)
            self.log.debug("%s", cursor._executed)
            if cursor.rowcount > 0:
                (transaction_id, ) = cursor.fetchone()
                deferred = self.cashInCurrencyCommit(transaction_id, packet)
            else:
                #
                # Get the currency note from the safe
                #
                sql = "SELECT name, serial, value FROM safe WHERE currency_serial = %s"
                cursor.execute(sql,(packet.currency_serial,))
                self.log.debug("%s", cursor._executed)
                if cursor.rowcount not in (0, 1):
                    message = "%s found %d records instead of 0 or 1" % (cursor._executed, cursor.rowcount)
                    self.log.error("%s", message)
                    raise PacketError(
                        other_type = PACKET_POKER_CASH_IN,
                        code = PacketPokerCashIn.SAFE,
                        message = message
                    )
                notes = [ (packet.url, packet.bserial, packet.name, packet.value) ]
                if cursor.rowcount == 1:
                    #
                    # A note already exists in the safe, merge it
                    # with the provided note
                    #
                    (name, serial, value) = cursor.fetchone()
                    notes.append((packet.url, serial, name, value))
                deferred = self.currency_client.meltNotes(*notes)
                deferred.addCallback(self.cashInUpdateCounter, packet, notes)
        finally:
            cursor.close()
        return deferred

    def cashOutCollect(self, currency_serial, transaction_id):
        cursor = self.db.cursor()
        if transaction_id:
            transaction = "counter.transaction_id = '" + str(transaction_id) + "' AND "
        else:
            transaction = ""

        sql = ( "SELECT counter.user_serial, currencies.url, counter.serial, counter.name, counter.value, counter.application_data FROM counter,currencies " + #pragma: no cover
                "       WHERE currencies.serial = " + str(currency_serial) + " AND " + #pragma: no cover
                "             counter.currency_serial = " + str(currency_serial) + " AND " + #pragma: no cover
                transaction + #pragma: no cover
                "             counter.status = 'c' " ) #pragma: no cover
        cursor.execute(sql)
        self.log.debug("%s", cursor._executed)
        if cursor.rowcount == 0:
            return None
        ( serial, url, bserial, name, value, application_data ) = cursor.fetchone()
        cursor.close()
        packet = PacketPokerCashOut(serial = serial,
                                    url = url,
                                    bserial = bserial,
                                    name = name,
                                    value = value,
                                    application_data = application_data)
        self.log.debug("cashOutCollect %s", packet)
        return packet
        
    def cashOutUpdateSafe(self, result, currency_serial, transaction_id):
        self.log.debug("cashOutUpdateSafe: %s %s", currency_serial, transaction_id)
        packet = self.cashOutCollect(currency_serial, transaction_id)
        if not packet:
            cursor = self.db.cursor()
            cursor.execute("START TRANSACTION")
            try:
                zero_or_one = lambda numrows: (numrows == 0 or numrows == 1)
                one = lambda numrows: numrows == 1
                sqls = []
                sqls.append(( "DELETE FROM safe WHERE currency_serial = %s" % currency_serial, one))
                sqls.append(( "INSERT INTO safe SELECT currency_serial, serial, name, value FROM counter " +
                              "       WHERE currency_serial = " + str(currency_serial) + " AND " +
                              "             status = 'r' ", zero_or_one))
                sqls.append(( "DELETE FROM counter WHERE currency_serial = %s and status = 'r'" % currency_serial, zero_or_one))
                sqls.append(( "UPDATE counter SET status = 'c' WHERE currency_serial = %s " % currency_serial , one))
                for ( sql, numrowsp ) in sqls:
                    self.log.debug("%s", sql)
                    if not numrowsp(cursor.execute(sql)):
                        message = sql + " affected " + str(cursor.rowcount) + " records "
                        self.log.error("%s", message)
                        raise PacketError(other_type = PACKET_POKER_CASH_OUT,
                                          code = PacketPokerCashOut.SAFE,
                                          message = message)

                packet = self.cashOutCollect(currency_serial, transaction_id)
                sql = ( "UPDATE user2money SET amount = amount - " + str(packet.value) + #pragma: no cover
                        "       WHERE user_serial = " + str(packet.serial) + " AND " + #pragma: no cover
                        "             currency_serial = " + str(currency_serial) ) #pragma: no cover
                if cursor.execute(sql) != 1:
                    message = sql + " affected " + str(cursor.rowcount) + " records instead of 1 "
                    self.log.error("%s", message)
                    raise PacketError(other_type = PACKET_POKER_CASH_OUT,
                                      code = PacketPokerCashOut.SAFE,
                                      message = message)
                cursor.execute("COMMIT")
                cursor.close()
            except:
                cursor.execute("ROLLBACK")
                cursor.close()
                raise
            if not packet:
                packet = PacketError(other_type = PACKET_POKER_CASH_OUT,
                                     code = PacketPokerCashOut.EMPTY,
                                     message = "no currency note to be collected for currency %d" % currency_serial)                
        self.unlock(currency_serial);
        return packet

    def cashOutCurrencyCommit(self, transaction_id, url):
        self.log.debug("cashOutCurrencyCommit")
        currency_serial = self.getCurrencySerial(url)
        deferred = self.currency_client.commit(url, transaction_id)
        deferred.addCallback(self.cashOutUpdateSafe, currency_serial, transaction_id)
        return deferred
        
    def cashOutUpdateCounter(self, new_notes, packet):
        self.log.debug("cashOutUpdateCounter: new_notes = %s packet = %s", new_notes, packet)
        cursor = self.db.cursor()
        if len(new_notes) != 2:
            raise PacketError(other_type = PACKET_POKER_CASH_OUT,
                              code = PacketPokerCashOut.BREAK_NOTE,
                              message = "breaking %s resulted in %d notes (%s) instead of 2" % ( packet, len(new_notes), str(new_notes) ) )
        if new_notes[0][3] == packet.value:
            user_note = new_notes[0]
            server_note = new_notes[1]
        elif new_notes[1][3] == packet.value:
            user_note = new_notes[1]
            server_note = new_notes[0]
        else:
            raise PacketError(other_type = PACKET_POKER_CASH_OUT,
                              code = PacketPokerCashOut.BREAK_NOTE,
                              message = "breaking %s did not provide a note with the proper value (notes are %s)" % ( packet, str(new_notes) ) )
        transaction_id = new_notes[0][2]
        url = new_notes[0][0]
        cursor.execute("START TRANSACTION")
        try:
            sql = ( "INSERT INTO counter ( transaction_id, user_serial, currency_serial, serial, name, value, status, application_data ) VALUES " +
                    "                    ( %s,             %s,          %s,              %s,     %s,   %s,    %s,     %s )" )
            #
            # Just forget about a zero value note that is provided by
            # the currencyclient for the sake of code consistency
            #
            if int(server_note[3]) > 0:
                cursor.execute(sql, ( transaction_id, packet.serial, packet.currency_serial, server_note[1], server_note[2], server_note[3], 'r', packet.application_data ))
            cursor.execute(sql, ( transaction_id, packet.serial, packet.currency_serial, user_note[1], user_note[2], user_note[3], 'u', packet.application_data ))
            cursor.execute("COMMIT")
            cursor.close()
        except:
            cursor.execute("ROLLBACK")
            cursor.close()
            raise

        return transaction_id

    def cashOutBreakNote(self, lock_name, packet):
        #
        # Ask the currency server to split the note in two
        #
        cursor = self.db.cursor()
        try:
            sql = "SELECT transaction_id FROM counter WHERE currency_serial = %s"
            cursor.execute(sql,(packet.currency_serial,))
            self.log.debug("%s", cursor._executed)
            if cursor.rowcount > 0:
                (transaction_id, ) = cursor.fetchone()
                deferred = self.cashOutCurrencyCommit(transaction_id, packet.url)
            else:
                #
                # Get the currency note from the safe
                #
                sql = "SELECT name, serial, value FROM safe WHERE currency_serial = %s"
                cursor.execute(sql,(packet.currency_serial,))
                self.log.debug("%s", cursor._executed)
                if cursor.rowcount != 1:
                    message = "%s found %d records instead of exactly 1" % (cursor._executed, cursor.rowcount)
                    self.log.error("%s", message)
                    raise PacketError(
                        other_type = PACKET_POKER_CASH_OUT,
                        code = PacketPokerCashOut.SAFE,
                        message = message
                    )
                (name, serial, value) = cursor.fetchone()
                note = (packet.url, serial, name, value)
                remainder = value - packet.value
                #
                # Break the note in two
                #
                deferred = self.currency_client.breakNote(note, remainder, packet.value)
                deferred.addCallback(self.cashOutUpdateCounter, packet)
                deferred.addCallback(self.cashOutCurrencyCommit, packet.url)
        finally:
            cursor.close()
        return deferred

    def getLockName(self, serial):
        return "cash_%d" % serial

    def unlock(self, currency_serial):
        name = self.getLockName(currency_serial)
        if name not in self.locks:
            self.log.warn("cashInUnlock: unexpected missing %s in locks (ignored)", name)
            return
        if not self.locks[name].isAlive():
            self.log.warn("cashInUnlock: unexpected dead %s pokerlock (ignored)", name)
            return
        self.locks[name].release(name)

    def lock(self, currency_serial):
        name = self.getLockName(currency_serial)

        self.log.debug("get lock %s", name)
        if name in self.locks:
            lock = self.locks[name]
            if lock.isAlive():
                create_lock = False
            else:
                lock.close()
                create_lock = True
        else:
            create_lock = True
        if create_lock:
            self.locks[name] = PokerLock(self.db_parameters)
            self.locks[name].start()

        return self.locks[name].acquire(name, int(self.parameters.get('acquire_timeout', 60)))
        
    def cashIn(self, packet):
        self.log.debug("cashIn: %s", packet)
        currency_serial = self.getCurrencySerial(packet.url)
        packet.currency_serial = currency_serial
        d = self.lock(currency_serial)
        d.addCallback(self.cashInValidateNote, packet)
        d.addErrback(self.cashGeneralFailure, packet)
        return d
    
    def cashOut(self, packet):
        self.log.debug("cashOut: %s", packet)
        currency_serial = self.getCurrencySerial(packet.url)
        packet.currency_serial = currency_serial
        d = self.lock(currency_serial)
        d.addCallback(self.cashOutBreakNote, packet)
        d.addErrback(self.cashGeneralFailure, packet)
        return d

    def cashOutCommit(self, packet):
        self.log.debug("cashOutCommit: %s", packet)
        cursor = self.db.cursor()
        cursor.execute("DELETE FROM counter WHERE name = %s AND status = 'c'", packet.transaction_id)
        cursor.close()
        return cursor.rowcount

    def cashQuery(self, packet):
        self.log.debug("cashQuery: %s", packet)
        cursor = self.db.cursor()
        sql = ( "SELECT COUNT(*) FROM counter WHERE " +
                " application_data = '" + str(packet.application_data) + "'" )
        cursor.execute(sql)
        self.log.debug("%s", cursor._executed)
        (count,) = cursor.fetchone()
        cursor.close()
        if count > 0:
            return PacketAck()
        else:
            return PacketError(other_type = PACKET_POKER_CASH_QUERY,
                               code = PacketPokerCashQuery.DOES_NOT_EXIST,
                               message = "No record with application_data = '%s'" % packet.application_data)
    
