"""
Twisted STOMP client

Copyright 2011 Mozes, Inc.

   Licensed under the Apache License, Version 2.0 (the "License");
   you may not use this file except in compliance with the License.
   You may obtain a copy of the License at

       http://www.apache.org/licenses/LICENSE-2.0

   Unless required by applicable law or agreed to in writing, software
   distributed under the License is distributed on an "AS IS" BASIS,
   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either expressed or implied.
   See the License for the specific language governing permissions and
   limitations under the License.
"""
import logging

from twisted.internet import defer, reactor
from twisted.internet.error import ConnectionLost
from twisted.internet.protocol import Factory, Protocol

from stompest.error import StompConnectTimeout, StompFrameError, StompProtocolError, StompConnectionError
from stompest.protocol import commands
from stompest.protocol.failover import StompFailoverUri
from stompest.protocol.frame import StompFrame
from stompest.protocol.parser import StompParser
from stompest.protocol.spec import StompSpec
from stompest.util import cloneStompMessage as _cloneStompMessage

from stompest.async.util import endpointFactory
from stompest.protocol.session import StompSession
import functools

LOG_CATEGORY = 'stompest.async.client'

class StompClient(Protocol):
    """A Twisted implementation of a STOMP client"""
    MESSAGE_INFO_LENGTH = 20
    CLIENT_ACK_MODES = set(['client', 'client-individual'])
    DEFAULT_ACK_MODE = 'client'
    
    def __init__(self, session=None, alwaysDisconnectOnUnhandledMsg=False):
        self._session = session or StompSession()
        self._alwaysDisconnectOnUnhandledMsg = alwaysDisconnectOnUnhandledMsg
        
        # leave the used logger public in case the user wants to override it
        self.log = logging.getLogger(LOG_CATEGORY)
        
        self._handlers = {
            'MESSAGE': self._handleMessage,
            'CONNECTED': self._handleConnected,
            'ERROR': self._handleError,
            'RECEIPT': self._handleReceipt,
        }
        self._subscriptions = {}
        self._connectedDeferred = None
        self._connectTimeoutDelayedCall = None
        self._connectError = None
        self._disconnectedDeferred = None
        self._finishedHandlersDeferred = None
        self._disconnecting = False
        self._disconnectError = None
        self._activeHandlers = set()
        self._parser = StompParser()

    #
    # user interface
    #
    def connect(self, login, passcode, timeout):
        """Send connect command and return Deferred for caller that will get trigger when connect is complete
        """
        if timeout is not None:
            self._connectTimeoutDelayedCall = reactor.callLater(timeout, self._connectTimeout, timeout) #@UndefinedVariable
        self._connect(login, passcode)
        self._connectedDeferred = defer.Deferred()
        return self._connectedDeferred
    
    def disconnect(self, failure=None):
        """After finishing outstanding requests, send disconnect command and return Deferred for caller that will get trigger when disconnect is complete
        """
        if failure:
            self._disconnectError = failure
        if not self._disconnecting:
            self._disconnecting = True
            #Send disconnect command after outstanding messages are ack'ed
            defer.maybeDeferred(self._finishHandlers).addBoth(lambda _: self._disconnect())
            
        return self._disconnectedDeferred
    
    def subscribe(self, dest, handler, headers=None, **kwargs):
        """Subscribe to a destination and register a function handler to receive messages for that destination
        """
        errorDestination = kwargs.get('errorDestination')
        frame = self._session.subscribe(dest, headers, context={'handler': handler, 'kwargs': kwargs})
        headers = frame.headers
        ack = headers.setdefault(StompSpec.ACK_HEADER, self.DEFAULT_ACK_MODE)
        token = self._session.token(frame)
        self._subscriptions[token] = {'destination': headers[StompSpec.DESTINATION_HEADER], 'handler': self._createHandler(handler), 'ack': ack, 'errorDestination': errorDestination}
        self.sendFrame(frame)
        return token
    
    def unsubscribe(self, subscription):
        frame = self._session.unsubscribe(subscription)
        token = self._session.token(frame)
        try:
            self._subscriptions.pop(token)
        except:
            self.log.warning('Cannot unsubscribe (subscription id unknown): %s=%s' % token)
        else:
            self.sendFrame(frame)
    
    def send(self, dest, msg='', headers=None):
        """Do the send command to enqueue a message to a destination
        """
        self.sendFrame(commands.send(dest, msg, headers))
    
    def sendFrame(self, message):
        frame = self._toFrame(message)
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug('Sending %s frame: %s%s' % (frame.cmd, repr(frame.headers), frame.body and ('[%s]' % repr('%s...' % frame.body[:self.MESSAGE_INFO_LENGTH]))))
        self._write(str(frame))
    
    def getDisconnectedDeferred(self):
        import warnings
        warnings.warn('StompClient.getDisconnectedDeferred() is deprecated. Use StompClient.disconnected instead!')
        return self.disconnected
    
    @property
    def disconnected(self):
        return self._disconnectedDeferred
    
    #
    # Overriden methods from parent protocol class
    #
        
    def connectionLost(self, reason):
        """When TCP connection is lost, remove shutdown handler
        """
        message = 'Disconnected'
        if reason.type is not ConnectionLost:
            message = '%s: %s' % (message, reason.getErrorMessage())
        self.log.debug(message)
        
        self._cancelConnectTimeout('Network connection was lost')
        self._handleConnectionLostConnect()
        self._handleConnectionLostDisconnect()
        
        Protocol.connectionLost(self, reason)
    
    def dataReceived(self, data):
        self._parser.add(data)
                
        while True:
            message = self._parser.getMessage()
            if not message:
                break
            try:
                handler = self._handlers[message['cmd']]
            except KeyError:
                raise StompFrameError('Unknown STOMP command: %s' % message)
            handler(message)
    
    #
    # Methods for sending raw STOMP commands
    #
    def _connect(self, login, passcode):
        self.sendFrame(commands.connect(login, passcode))

    def _disconnect(self):
        self.sendFrame(commands.disconnect())
        self.transport.loseConnection()
        if not self._disconnectError:
            list(self._session.replay()) # forget subscriptions upon graceful disconnect
            
    def _ack(self, messageId):
        self.sendFrame(commands.ack({StompSpec.MESSAGE_ID_HEADER: messageId}))
    
    def _toFrame(self, message):
        if not isinstance(message, StompFrame):
            message = StompFrame(**message)
        return message
    
    def _write(self, data):
        #self.log.debug('sending data:\n%s' % repr(data))
        self.transport.write(data)

    #
    # Private helper methods
    #
    def _cancelConnectTimeout(self, reason):
        if not self._connectTimeoutDelayedCall:
            return
        self.log.debug('Cancelling connect timeout [%s]' % reason)
        self._connectTimeoutDelayedCall.cancel()
        self._connectTimeoutDelayedCall = None
    
    def _createHandler(self, handler):
        @functools.wraps(handler)
        def _handler(_, result):
            return handler(self, result)
        return _handler

    def _handleConnectionLostDisconnect(self):
        if not self._disconnectedDeferred:
            return
        if not self._disconnecting:
            self._disconnectError = StompConnectionError('Unexpected connection loss')
        if self._disconnectError:
            #self.log.debug('Calling disconnected deferred errback: %s' % self._disconnectError)
            self._disconnectedDeferred.errback(self._disconnectError)
            self._disconnectError = None
        else:
            #self.log.debug('Calling disconnected deferred callback')
            self._disconnectedDeferred.callback(self)
        self._disconnectedDeferred = None
            
    def _handleConnectionLostConnect(self):
        if not self._connectedDeferred:
            return
        if self._connectError:
            error, self._connectError = self._connectError, None
        else:
            self.log.error('Connection lost before connection was established')
            error = StompConnectionError('Unexpected connection loss')
        self.log.debug('Calling connected deferred errback: %s' % error)
        self._connectedDeferred.errback(error)                
        self._connectedDeferred = None
    
    def _finishHandlers(self):
        """Return a Deferred to signal when all requests in process are complete
        """
        if self._handlersInProgress():
            self._finishedHandlersDeferred = defer.Deferred()
            return self._finishedHandlersDeferred
    
    def _handlersInProgress(self):
        return bool(self._activeHandlers)
    
    def _handlerFinished(self, messageId):
        self._activeHandlers.remove(messageId)
        self.log.debug('Handler complete for message: %s' % messageId)

    def _handlerStarted(self, messageId):
        if messageId in self._activeHandlers:
            raise StompProtocolError('Duplicate message received. Message id %s is already in progress' % messageId)
        self._activeHandlers.add(messageId)
        self.log.debug('Handler started for message: %s' % messageId)
    
    def _messageHandlerFailed(self, failure, messageId, msg, errDest):
        self.log.error('Error in message handler: %s' % repr(failure))
        if errDest: #Forward message to error queue if configured
            errorMessage = _cloneStompMessage(msg, persistent=True)
            self.send(errDest, errorMessage['body'], errorMessage['headers'])
            self._ack(messageId)
            if not self._alwaysDisconnectOnUnhandledMsg:
                return
        self.disconnect(failure)

    def _connectTimeout(self, timeout):
        self.log.error('Connect command timed out after %s seconds' % timeout)
        self._connectTimeoutDelayedCall = None
        self._connectError = StompConnectTimeout('Connect command timed out after %s seconds' % timeout)
        self.transport.loseConnection()
    
    def _handleConnected(self, msg):
        """Handle STOMP CONNECTED commands
        """
        sessionId = msg['headers'].get('session')
        self.log.debug('Connected to stomp broker with session: %s' % sessionId)
        self._cancelConnectTimeout('successfully connected')
        self._disconnectedDeferred = defer.Deferred()
        self._replay()
        self._connectedDeferred.callback(self)
        self._connectedDeferred = None
    
    @defer.inlineCallbacks
    def _handleMessage(self, msg):
        """Handle STOMP MESSAGE commands
        """
        headers = msg['headers']
        messageId = headers[StompSpec.MESSAGE_ID_HEADER]
        try:
            token = self._session.token(headers)
            subscription = self._subscriptions[token]
        except:
            self.log.warning('Ignoring STOMP message (no handler found): %s [headers=%s]' % (messageId, headers))
            return
        
        #Do not process any more messages if we're disconnecting
        if self._disconnecting:
            self.log.debug('Ignoring STOMP message (disconnecting): %s [headers=%s]' % (messageId, headers))
            return
        
        if self.log.isEnabledFor(logging.DEBUG):
            self.log.debug('Received STOMP message: %s [headers=%s, body=%s...]' % (messageId, msg['headers'], msg['body'][:self.MESSAGE_INFO_LENGTH]))
        
        #Call message handler (can return deferred to be async)
        self._handlerStarted(messageId)
        try:
            yield defer.maybeDeferred(subscription['handler'], self, msg)
        except Exception as e:
            self._messageHandlerFailed(e, messageId, msg, subscription['errorDestination'])
        else:
            if self._isClientAck(subscription):
                self._ack(messageId)
        finally:
            self._postProcessMessage(messageId)
        
    def _isClientAck(self, subscription):
        return subscription['ack'] in self.CLIENT_ACK_MODES

    def _postProcessMessage(self, messageId):
        self._handlerFinished(messageId)
        self._finish()
        
    def _finish(self):
        #If someone's waiting to know that all handlers are done, call them back
        if (not self._finishedHandlersDeferred) or self._handlersInProgress():
            return
        self._finishedHandlersDeferred.callback(self)
        self._finishedHandlersDeferred = None
        
    def _replay(self):
        for (destination, headers, context) in self._session.replay():
            self.log.debug('Replaying subscription: %s' % headers)
            self.subscribe(destination, context['handler'], headers, **context['kwargs'])
            
    def _handleError(self, msg):
        """Handle STOMP ERROR commands
        """
        self.log.info('Received stomp error: %s' % msg)
        if self._connectedDeferred:
            self.transport.loseConnection()
            self._connectError = StompProtocolError('STOMP error message received while trying to connect: %s' % msg)
        else:
            #Workaround for AMQ < 5.2
            if 'Unexpected ACK received for message-id' in msg['headers'].get('message', ''):
                self.log.debug('AMQ brokers < 5.2 do not support client-individual mode.')
            else:
                self._disconnectError = StompProtocolError('STOMP error message received: %s' % msg)
                self.disconnect()
        
    def _handleReceipt(self, msg):
        """Handle STOMP RECEIPT commands
        """
        self.log.info('Received stomp receipt: %s' % msg)

class StompFactory(Factory):
    protocol = StompClient
    
    def __init__(self, **kwargs):
        self._kwargs = kwargs
        
    def buildProtocol(self, _):
        protocol = self.protocol(**self._kwargs)
        protocol.factory = self
        return protocol

class StompCreator(object):
    def __init__(self, config, connectTimeout=None, **kwargs):
        self.config = config
        self.connectTimeout = connectTimeout
        self.kwargs = kwargs
        self.log = logging.getLogger(LOG_CATEGORY)
    
    @defer.inlineCallbacks  
    def getConnection(self, endpoint=None):
        endpoint = endpoint or self._createEndpoint()
        stomp = yield endpoint.connect(StompFactory(**self.kwargs))
        yield stomp.connect(self.config.login, self.config.passcode, timeout=self.connectTimeout)
        defer.returnValue(stomp)
        
    def _createEndpoint(self):
        brokers = StompFailoverUri(self.config.uri).brokers
        if len(brokers) != 1:
            raise ValueError('failover URI is not supported [%s]' % self.config.failoverUri)
        return endpointFactory(brokers[0])