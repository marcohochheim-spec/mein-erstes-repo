using System;
using ATAS.DataFeedsCore;

namespace OrderFlowAutoBot
{
    public partial class OrderFlowSignalStrategy
    {
        private Order _stopOrder;
        private Order _targetOrder;
        private bool _breakevenApplied;

        private DateTime _currentTradingDay = DateTime.MinValue;
        private int _tradesToday;
        private decimal _dayStartRealizedPnL;
        private int _lastPositionCloseBar = -1;
        private decimal _previousPosition;

        private bool IsWithinSession(DateTime time)
        {
            if (!UseSessionFilter)
                return true;

            var t = time.TimeOfDay;

            if (SessionStart <= SessionEnd)
                return t >= SessionStart && t <= SessionEnd;

            // Session ueber Mitternacht hinweg (z.B. 22:00 - 06:00)
            return t >= SessionStart || t <= SessionEnd;
        }

        private decimal GetCurrentRealizedPnL()
        {
            if (TradingManager == null || TradingManager.Position == null)
                return 0;

            return TradingManager.Position.RealizedPnL;
        }

        private void RollTradingDayIfNeeded(DateTime barTime)
        {
            var day = barTime.Date;

            if (day == _currentTradingDay)
                return;

            _currentTradingDay = day;
            _tradesToday = 0;
            _dayStartRealizedPnL = GetCurrentRealizedPnL();
        }

        private bool DailyLimitsAllowTrading()
        {
            if (_tradesToday >= MaxTradesPerDay)
                return false;

            if (UseMaxDailyLoss)
            {
                var realizedToday = GetCurrentRealizedPnL() - _dayStartRealizedPnL;

                if (realizedToday <= -MaxDailyLossMoney)
                    return false;
            }

            return true;
        }

        private bool CooldownElapsed(int bar)
        {
            if (_lastPositionCloseBar < 0)
                return true;

            return bar - _lastPositionCloseBar >= CooldownBarsAfterTrade;
        }

        private bool DirectionAllowed(int direction)
        {
            if (direction > 0)
                return TradeDirection != TradeDirectionMode.ShortOnly;

            if (direction < 0)
                return TradeDirection != TradeDirectionMode.LongOnly;

            return false;
        }

        private void TryEnter(int bar, int direction)
        {
            if (direction == 0)
                return;

            if (CurrentPosition != 0)
                return;

            if (!DirectionAllowed(direction))
                return;

            if (!DailyLimitsAllowTrading())
                return;

            if (!CooldownElapsed(bar))
                return;

            var order = new Order
            {
                Portfolio = Portfolio,
                Security = Security,
                Direction = direction > 0 ? OrderDirections.Buy : OrderDirections.Sell,
                Type = OrderTypes.Market,
                QuantityToFill = OrderVolume,
                Comment = direction > 0 ? "OrderFlowBot Long Entry" : "OrderFlowBot Short Entry"
            };

            OpenOrder(order);
        }

        // Wird von OnCurrentPositionChanged aufgerufen, sobald eine neue
        // Position eroeffnet wurde: platziert Stop- und Zielorder als Bracket.
        private void PlaceBracketOrders(int bar)
        {
            CancelBracketOrders();
            _breakevenApplied = false;

            if (CurrentPosition == 0)
                return;

            var isLong = CurrentPosition > 0;
            var entryPrice = AveragePrice;
            var stopDistance = GetStopDistancePoints(bar);
            var targetDistance = GetTargetDistancePoints(stopDistance);

            var stopPrice = isLong ? entryPrice - stopDistance : entryPrice + stopDistance;
            var targetPrice = isLong ? entryPrice + targetDistance : entryPrice - targetDistance;
            var exitDirection = isLong ? OrderDirections.Sell : OrderDirections.Buy;
            var qty = Math.Abs(CurrentPosition);
            var ocoGroup = Guid.NewGuid().ToString();

            _stopOrder = new Order
            {
                Portfolio = Portfolio,
                Security = Security,
                Direction = exitDirection,
                Type = OrderTypes.Stop,
                TriggerPrice = RoundToTick(stopPrice),
                QuantityToFill = qty,
                OCOGroup = ocoGroup,
                Comment = "OrderFlowBot Stop"
            };

            _targetOrder = new Order
            {
                Portfolio = Portfolio,
                Security = Security,
                Direction = exitDirection,
                Type = OrderTypes.Limit,
                Price = RoundToTick(targetPrice),
                QuantityToFill = qty,
                OCOGroup = ocoGroup,
                Comment = "OrderFlowBot Target"
            };

            OpenOrder(_stopOrder);
            OpenOrder(_targetOrder);
        }

        private void CancelBracketOrders()
        {
            if (_stopOrder != null)
            {
                CancelOrder(_stopOrder);
                _stopOrder = null;
            }

            if (_targetOrder != null)
            {
                CancelOrder(_targetOrder);
                _targetOrder = null;
            }
        }

        private decimal GetStopDistancePoints(int bar)
        {
            if (StopLossMode == StopLossMode.AtrMultiple)
            {
                var atr = GetAtrValue(bar);
                return atr * AtrMultiplierSl;
            }

            return StopLossTicks * InstrumentInfo.TickSize;
        }

        private decimal GetTargetDistancePoints(decimal stopDistancePoints)
        {
            switch (TakeProfitMode)
            {
                case TakeProfitMode.RiskRewardRatio:
                    return stopDistancePoints * RiskRewardRatio;
                case TakeProfitMode.AtrMultiple:
                    return stopDistancePoints / AtrMultiplierSl * AtrMultiplierTp;
                default:
                    return TakeProfitTicks * InstrumentInfo.TickSize;
            }
        }

        private decimal RoundToTick(decimal price)
        {
            if (InstrumentInfo.TickSize <= 0)
                return price;

            return Math.Round(price / InstrumentInfo.TickSize) * InstrumentInfo.TickSize;
        }

        // Verschiebt den Stop auf Breakeven (+Offset) sobald der konfigurierte
        // Gewinn-Trigger erreicht ist, und trailt optional dahinter her.
        private void ManageOpenPosition(int bar)
        {
            if (CurrentPosition == 0 || _stopOrder == null)
                return;

            var isLong = CurrentPosition > 0;
            var candle = GetCandle(bar);
            var currentPrice = candle.Close;
            var entryPrice = AveragePrice;

            var profitPoints = isLong ? currentPrice - entryPrice : entryPrice - currentPrice;
            var profitTicks = InstrumentInfo.TickSize > 0 ? profitPoints / InstrumentInfo.TickSize : 0;

            decimal? newStopPrice = null;

            if (UseBreakeven && !_breakevenApplied && profitTicks >= BreakevenTriggerTicks)
            {
                var offset = BreakevenOffsetTicks * InstrumentInfo.TickSize;
                newStopPrice = isLong ? entryPrice + offset : entryPrice - offset;
                _breakevenApplied = true;
            }

            if (UseTrailingStop && profitTicks >= TrailingStopTicks)
            {
                var trailDistance = TrailingStopTicks * InstrumentInfo.TickSize;
                var trailedStop = isLong ? currentPrice - trailDistance : currentPrice + trailDistance;

                var isBetter = newStopPrice == null
                    ? true
                    : (isLong ? trailedStop > newStopPrice : trailedStop < newStopPrice);

                if (isBetter)
                {
                    var currentStop = _stopOrder.TriggerPrice;
                    var improves = isLong ? trailedStop > currentStop : trailedStop < currentStop;

                    if (improves)
                        newStopPrice = trailedStop;
                }
            }

            if (newStopPrice == null)
                return;

            var rounded = RoundToTick(newStopPrice.Value);

            if (rounded == _stopOrder.TriggerPrice)
                return;

            var updatedStop = _stopOrder.Clone();
            updatedStop.TriggerPrice = rounded;
            ModifyOrder(_stopOrder, updatedStop);
            _stopOrder = updatedStop;
        }

        protected override void OnCurrentPositionChanged()
        {
            base.OnCurrentPositionChanged();

            var previousPosition = _previousPosition;
            _previousPosition = CurrentPosition;

            var bar = CurrentBar - 1;

            if (previousPosition == 0 && CurrentPosition != 0)
            {
                PlaceBracketOrders(bar);
            }
            else if (previousPosition != 0 && CurrentPosition == 0)
            {
                CancelBracketOrders();
                _tradesToday++;
                _lastPositionCloseBar = bar;
            }
        }

        protected override void OnStopping()
        {
            CancelBracketOrders();

            if (CurrentPosition != 0)
            {
                var order = new Order
                {
                    Portfolio = Portfolio,
                    Security = Security,
                    Direction = CurrentPosition > 0 ? OrderDirections.Sell : OrderDirections.Buy,
                    Type = OrderTypes.Market,
                    QuantityToFill = Math.Abs(CurrentPosition),
                    Comment = "OrderFlowBot Flatten on Stop"
                };

                OpenOrder(order);
            }

            base.OnStopping();
        }
    }
}
