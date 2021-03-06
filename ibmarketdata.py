import datetime
import json
import queue
import decimal
import uuid
import inspect
import distutils.util
import time
import argparse
import boto3
import ibapi.wrapper
from botocore.exceptions import ClientError
from ibapi import (comm)
from ibapi.client import EClient
from ibapi.common import *
from ibapi.contract import *
from ibapi.contract import Contract
from ibapi.errors import *
from ibapi.ticktype import TickType, TickTypeEnum
from ibapi.utils import *
from ibapi.utils import (BadMessage)


# Helper class to convert a DynamoDB item to JSON.
class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        return super(DecimalEncoder, self).default(o)


class Utils(object):
    def __init__(self):
        pass

    @staticmethod
    def reliable(func):
        def _decorator(self, *args, **kwargs):
            tries = 0
            result = func(self, *args, **kwargs)
            if result is None:
                while result is None and tries < 10:
                    tries += 1
                    time.sleep(2 ** tries)
                    result = func(self, *args, **kwargs)
            return result

        return _decorator


class InterruptableClient(EClient):
    def __init__(self):
        EClient.__init__(self, self)
        self.lastStamp = datetime.datetime.utcnow()

    def runnable(self, func):
        """This is the function that has the message loop."""

        try:
            while not self.done and (self.conn.isConnected()
                                     or not self.msg_queue.empty()):
                try:
                    try:
                        text = self.msg_queue.get(block=True, timeout=0.2)
                        if len(text) > MAX_MSG_LEN:
                            self.wrapper.error(NO_VALID_ID, BAD_LENGTH.code(),
                                               "%s:%d:%s" % (BAD_LENGTH.msg(), len(text), text))
                            self.disconnect()
                            break
                    except queue.Empty:
                        if datetime.datetime.utcnow() - self.lastStamp > datetime.timedelta(seconds=30):
                            func()
                            self.lastStamp = datetime.datetime.utcnow()
                        logging.debug("queue.get: empty")
                    else:
                        fields = comm.read_fields(text)
                        logging.debug("fields %s", fields)
                        self.decoder.interpret(fields)
                except (KeyboardInterrupt, SystemExit):
                    logging.info("detected KeyboardInterrupt, SystemExit")
                    self.keyboardInterrupt()
                    self.keyboardInterruptHard()
                except BadMessage:
                    logging.info("BadMessage")
                    self.conn.disconnect()

                logging.debug("conn:%d queue.sz:%d",
                              self.conn.isConnected(),
                              self.msg_queue.qsize())
        finally:
            self.disconnect()


class CloudLogger(object):
    def __init__(self):
        self.__fileLogger = logging.getLogger()
        self.__fileLogger.setLevel(logging.INFO)
        logging.basicConfig(format='%(asctime)s - %(levelname)s - %(threadName)s - %(message)s')
        self.__cloudWatchLogger = boto3.client('logs')
        self.__groupName = '/aws/docker/Capsule'
        self.__sequenceToken = None
        self.__stream = (datetime.datetime.today().strftime('%Y/%m/%d/[$LATEST]'), uuid.uuid4().hex)
        response = self.__cloudWatchLogger.create_log_stream(
            logGroupName=self.__groupName,
            logStreamName='%s%s' % self.__stream
        )
        self.info('LogStream Created: %s' % response)

    def __logToStream(self, msg):
        if self.__sequenceToken is None:
            response = self.__cloudWatchLogger\
                .put_log_events(logGroupName=self.__groupName, logStreamName='%s%s' % self.__stream,
                                logEvents=[dict(timestamp=int(round(time.time() * 1000)),
                                                message=time.strftime("%m/%d/%Y %H:%M:%S") + msg)])
        else:
            response = self.__cloudWatchLogger\
                .put_log_events(logGroupName=self.__groupName, logStreamName='%s%s' % self.__stream,
                                logEvents=[dict(timestamp=int(round(time.time() * 1000)),
                                                message=msg)],
                                sequenceToken=self.__sequenceToken)
        self.__sequenceToken = response['nextSequenceToken']

    def info(self, msg):
        self.__fileLogger.info(msg)
        name = inspect.getframeinfo(inspect.currentframe()).function.upper()
        self.__logToStream('%s [%s] %s' % (time.strftime("%m/%d/%Y %H:%M:%S"), name,  msg))

    def debug(self, msg):
        self.__fileLogger.debug(msg)
        name = inspect.getframeinfo(inspect.currentframe()).function.upper()
        self.__logToStream('%s [%s] %s' % (time.strftime("%m/%d/%Y %H:%M:%S"), name, msg))

    def warning(self, msg):
        self.__fileLogger.warning(msg)
        name = inspect.getframeinfo(inspect.currentframe()).function.upper()
        self.__logToStream('%s [%s] %s' % (time.strftime("%m/%d/%Y %H:%M:%S"), name, msg))

    def error(self, msg):
        self.__fileLogger.error(msg)
        name = inspect.getframeinfo(inspect.currentframe()).function.upper()
        self.__logToStream('%s [%s] %s' % (time.strftime("%m/%d/%Y %H:%M:%S"), name, msg))


class IbApp(InterruptableClient, ibapi.wrapper.EWrapper):
    def __init__(self, real, hist):
        self.__subRealMD = real
        self.__subHistMD = hist
        self.Logger = CloudLogger()
        InterruptableClient.__init__(self)
        self.nextValidOrderId = None
        self.nextValidReqId = None
        self.requestedContracts = {}
        self.requestedMarketData = {}
        self.requestedHistoricalData = {}
        self.marketDataLookup = {}
        self.historicalLookup = {}
        db = boto3.resource('dynamodb', region_name='us-east-1')
        self.__Securities = db.Table('Securities')
        self.__QuotesEod = db.Table('Quotes.EOD')

    def __del__(self):
        self.disconnect()

    @Utils.reliable
    def getSecurities(self):
        try:
            self.Logger.info('Calling securities scan ...')
            response = self.__Securities.scan()
        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
            return None
        except Exception as e:
            self.Logger.error(e)
            return None
        else:
            # self.Logger.info(json.dumps(security, indent=4, cls=DecimalEncoder))
            if 'Items' in response:
                return response['Items']

    def UpdateQuote(self, symbol, date, opn, close, high, low, volume, barCount):
        try:
            details = {"Open": decimal.Decimal(str(opn)), "Close": decimal.Decimal(str(close)),
                       "High": decimal.Decimal(str(high)), "Low": decimal.Decimal(str(low)),
                       "Volume": volume, "Count": barCount}
            response = self.__QuotesEod.update_item(
                Key={
                    'Symbol': symbol,
                    'Date': date,
                },
                UpdateExpression="set #d = :d, #s = :s",
                ExpressionAttributeNames={
                    '#d': 'Details',
                    '#s': 'Source',
                },
                ExpressionAttributeValues={
                    ':d': details,
                    ':s': 'IB',
                },
                ReturnValues="UPDATED_NEW")

        except ClientError as e:
            self.Logger.error(e.response['Error']['Message'])
        except Exception as e:
            self.Logger.error(e)
        else:
            self.Logger.debug(json.dumps(response, indent=4, cls=DecimalEncoder))

    def verify(self):
        self.Logger.info('requesting server time')
        self.reqCurrentTime()
        for key, value in self.requestedContracts.items():
            self.reqContractDetails(key, value)
            self.Logger.info('re-requesting contract details for: %s' % value.symbol)

        if self.__subHistMD:
            for key, value in self.requestedHistoricalData.items():
                self.reqHistoricalData(key, value, '', "2 D", "1 day", "TRADES", 1, 1, False, list("XYZ"))
                self.Logger.info('re-requesting Historical Data for: %s' % value.symbol)

        if self.__subRealMD:
            for key, value in self.requestedMarketData.items():
                self.reqMktData(key, value, "", True, False, [])
                self.Logger.info('re-requesting Market Data for: %s' % value.symbol)

    def loop(self):
        self.runnable(self.verify)

    def start(self):
        self.Logger.info('start for read data %s and historical %s' % (self.__subRealMD, self.__subHistMD))
        items = self.getSecurities()

        for sec in items:
            if sec['SubscriptionEnabled']:
                contract = Contract()
                contract.symbol = sec['Symbol']
                contract.secType = sec['ProductType']
                contract.exchange = sec['Description']['Exchange']
                if contract.secType == 'FUT':
                    contract.tradingClass = sec['Symbol']
                rId = self.nextReqId()
                self.requestedContracts[rId] = contract
                self.reqContractDetails(rId, contract)

    def nextReqId(self):
        reqId = self.nextValidReqId
        self.nextValidReqId += 1
        return reqId

    def nextOrderId(self):
        orderId = self.nextValidOrderId
        self.nextValidOrderId += 1
        return orderId

    @iswrapper
    def contractDetails(self, reqId: int, contractDetails: ContractDetails):
        super(IbApp, self).contractDetails(reqId, contractDetails)
        self.Logger.info('contractDetails received %s ' % contractDetails.summary)
        if reqId not in self.requestedContracts:
            self.Logger.warning('Unknown contractDetails reqId: %s' % reqId)
            return
        contract = self.requestedContracts[reqId]
        if contract.symbol == contractDetails.summary.symbol or contract.symbol == contractDetails.marketName:
            validated = Contract()
            validated.symbol = contractDetails.summary.symbol
            validated.secType = contractDetails.summary.secType
            validated.exchange = contractDetails.summary.exchange
            validated.tradingClass = contractDetails.summary.tradingClass
            validated.lastTradeDateOrContractMonth = contractDetails.summary.lastTradeDateOrContractMonth
            validated.localSymbol = contractDetails.summary.localSymbol

            if self.__subRealMD:
                cId = self.nextReqId()
                self.marketDataLookup[cId] = validated.localSymbol
                self.requestedMarketData[cId] = validated
                self.reqMktData(cId, contract, "", True, False, [])

            if self.__subHistMD:
                hId = self.nextReqId()
                self.historicalLookup[hId] = validated.localSymbol
                self.requestedHistoricalData[hId] = validated
                self.reqHistoricalData(hId, validated, '', "2 D", "1 day", "TRADES", 1, 1, False, list("XYZ"))

        else:
            self.Logger.warning('Unknown contract received %s' % contractDetails.summary)

    @iswrapper
    def contractDetailsEnd(self, reqId: int):
        super(IbApp, self).contractDetailsEnd(reqId)
        self.Logger.info("ContractDetailsEnd. %s" % reqId)
        if reqId in self.requestedContracts:
            del self.requestedContracts[reqId]

    @iswrapper
    def historicalData(self, reqId: TickerId, bar: BarData):
        sym = self.historicalLookup[reqId]

        self.Logger.info("HistoricalData. " + sym + " Date: " + bar.date + " Open: " + str(bar.open) +
                         " High: " + str(bar.high) + " Low: " + str(bar.low) + " Close: " + str(bar.close) + " Volume: "
                         + str(bar.volume) + " Count: " + str(bar.barCount))
        if reqId in self.requestedHistoricalData:
            del self.requestedHistoricalData[reqId]

        self.UpdateQuote(sym, bar.date, bar.open, bar.close, bar.high, bar.low, bar.volume, bar.barCount)

    @iswrapper
    def historicalDataEnd(self, reqId: int, start: str, end: str):
        super(IbApp, self).historicalDataEnd(reqId, start, end)
        self.Logger.info("HistoricalDataEnd " + str(reqId) + " from " + start + " to " + end)

    @iswrapper
    def tickSnapshotEnd(self, reqId: int):
        super(IbApp, self).tickSnapshotEnd(reqId)
        self.Logger.info("TickSnapshotEnd: %s" % reqId)

    @iswrapper
    def nextValidId(self, orderId: int):
        super(IbApp, self).nextValidId(orderId)

        self.Logger.info("setting nextValidOrderId: %d" % orderId)
        self.nextValidOrderId = orderId
        self.nextValidReqId = orderId
        self.start()

    @iswrapper
    def marketDataType(self, reqId: TickerId, marketDataType: int):
        super(IbApp, self).marketDataType(reqId, marketDataType)
        self.Logger.info("MarketDataType. %s Type: %s" % (reqId, marketDataType))

    @iswrapper
    def error(self, *args):
        super(IbApp, self).error(*args)

    @iswrapper
    def winError(self, *args):
        super(IbApp, self).error(*args)

    @iswrapper
    def currentTime(self, tim: int):
        super(IbApp, self).currentTime(tim)
        self.Logger.info('currentTime: %s' % tim)

    @iswrapper
    def tickPrice(self, tickerId: TickerId, tickType: TickType, price: float, attrib):
        symbol = self.marketDataLookup[tickerId]
        self.Logger.info('%s %s %s %s IB' % (datetime.datetime.now(), symbol, TickTypeEnum.to_str(tickType), price))
        if tickerId in self.requestedMarketData:
            del self.requestedMarketData[tickerId]

    @iswrapper
    def tickSize(self, tickerId: TickerId, tickType: TickType, size: int):
        symbol = self.marketDataLookup[tickerId]
        self.Logger.info('%s %s %s %s IB' % (datetime.datetime.now(), symbol, TickTypeEnum.to_str(tickType), size))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--host', help='IB host', required=True)
    parser.add_argument('--port', help='IB port', type=int, required=True)
    parser.add_argument('--clientId', help='IB client id', type=int, required=True)
    parser.add_argument('--real', help='IB Market Data', type=lambda x: bool(distutils.util.strtobool(x)),
                        required=True)
    parser.add_argument('--hist', help='IB Historical', type=lambda x: bool(distutils.util.strtobool(x)), required=True)
    args = parser.parse_args()

    app = IbApp(args.real, args.hist)
    app.connect(args.host, args.port, args.clientId)
    app.Logger.info("serverVersion:%s connectionTime:%s" % (app.serverVersion(), app.twsConnectionTime()))

    app.loop()


if __name__ == "__main__":
    main()
