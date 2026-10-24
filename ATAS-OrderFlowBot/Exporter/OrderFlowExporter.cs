using System;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Globalization;
using System.IO;
using System.Text;
using ATAS.Indicators;

namespace OrderFlowAutoBot
{
    // Exportiert die Orderflow-Daten jedes ABGESCHLOSSENEN Bars des Charts als CSV,
    // damit sie ausserhalb von ATAS (strategy-tests/orderflow_study.py) analysiert werden koennen.
    //
    //   <Ordner>\<Instrument>_<Tag>_<Zeitstempel>_bars.csv    eine Zeile pro Bar (OHLC, Delta, POC, Value Area ...)
    //   <Ordner>\<Instrument>_<Tag>_<Zeitstempel>_levels.csv  Footprint: eine Zeile pro Bar und Preis (Bid/Ask/Volumen)
    //
    // Jede Neuberechnung des Charts (Neuladen, Zeitrahmen-Wechsel, Parameter-Aenderung) beginnt
    // ein neues Dateipaar. Der gerade laufende (offene) Bar wird erst exportiert, wenn er schliesst.
    // Der Indikator handelt nicht - er liest nur und zeichnet das Bar-Delta in ein eigenes Panel.
    [DisplayName("OrderFlow Exporter (CSV)")]
    public class OrderFlowExporter : Indicator
    {
        private string _folder = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments), "ATAS_OrderFlowExport");
        private string _tag = "export";
        private bool _exportLevels = true;

        private StreamWriter _bars;
        private StreamWriter _levels;
        private int _lastExportedBar = -1;
        private string _error;

        [Display(Name = "Export-Ordner", GroupName = "Export", Order = 10)]
        public string Folder
        {
            get { return _folder; }
            set { _folder = value; RecalculateValues(); }
        }

        [Display(Name = "Datei-Kennung (z.B. 1m, 500tick)", GroupName = "Export", Order = 20)]
        public string Tag
        {
            get { return _tag; }
            set { _tag = value; RecalculateValues(); }
        }

        [Display(Name = "Footprint je Preislevel exportieren", GroupName = "Export", Order = 30)]
        public bool ExportLevels
        {
            get { return _exportLevels; }
            set { _exportLevels = value; RecalculateValues(); }
        }

        public OrderFlowExporter()
            : base(true)
        {
            Panel = IndicatorDataProvider.NewPanel;
        }

        protected override void OnCalculate(int bar, decimal value)
        {
            if (bar == 0)
                OpenFiles();

            var candle = GetCandle(bar);
            this[bar] = candle.Delta;

            // Bar 'bar' hat begonnen -> alle davor sind abgeschlossen.
            if (_bars == null)
                return;

            var exported = false;

            while (_lastExportedBar < bar - 1)
            {
                _lastExportedBar++;
                WriteBar(_lastExportedBar);
                exported = true;
            }

            // Historie wird am Stueck geschrieben; im Live-Betrieb nach jedem neuen Bar sichern.
            if (exported && bar >= CurrentBar - 1)
                Flush();
        }

        protected override void OnDispose()
        {
            CloseFiles();
            base.OnDispose();
        }

        private void OpenFiles()
        {
            CloseFiles();
            _lastExportedBar = -1;
            _error = null;

            try
            {
                Directory.CreateDirectory(_folder);

                var instrument = InstrumentInfo != null ? InstrumentInfo.Instrument : "unknown";
                var stem = string.Format("{0}_{1}_{2:yyyyMMdd-HHmmss}",
                    SafeName(instrument), SafeName(_tag), DateTime.Now);

                _bars = new StreamWriter(Path.Combine(_folder, stem + "_bars.csv"), false, new UTF8Encoding(false));
                _bars.WriteLine("bar,time,last_time,open,high,low,close,volume,ask,bid,delta,max_delta,min_delta," +
                                "ticks,poc_price,poc_volume,vah,val,tick_size");

                if (_exportLevels)
                {
                    _levels = new StreamWriter(Path.Combine(_folder, stem + "_levels.csv"), false, new UTF8Encoding(false));
                    _levels.WriteLine("bar,time,price,bid,ask,volume,ticks");
                }
            }
            catch (Exception ex)
            {
                _error = ex.Message;
                CloseFiles();
                AddAlert("OrderFlowExporter", "Export fehlgeschlagen: " + _error);
            }
        }

        private void WriteBar(int bar)
        {
            var c = GetCandle(bar);
            var inv = CultureInfo.InvariantCulture;
            var time = c.Time.ToString("yyyy-MM-dd HH:mm:ss.fff", inv);

            decimal pocPrice = 0, pocVolume = 0;
            var poc = c.MaxVolumePriceInfo;
            if (poc != null)
            {
                pocPrice = poc.Price;
                pocVolume = poc.Volume;
            }

            decimal vah = 0, val = 0;
            var va = c.ValueArea;
            if (va != null)
            {
                vah = va.ValueAreaHigh;
                val = va.ValueAreaLow;
            }

            _bars.WriteLine(string.Format(inv,
                "{0},{1},{2},{3},{4},{5},{6},{7},{8},{9},{10},{11},{12},{13},{14},{15},{16},{17},{18}",
                bar, time, c.LastTime.ToString("yyyy-MM-dd HH:mm:ss.fff", inv),
                c.Open, c.High, c.Low, c.Close, c.Volume, c.Ask, c.Bid, c.Delta, c.MaxDelta, c.MinDelta,
                c.Ticks, pocPrice, pocVolume, vah, val, InstrumentInfo.TickSize));

            if (_levels == null)
                return;

            foreach (var level in c.GetAllPriceLevels())
            {
                _levels.WriteLine(string.Format(inv, "{0},{1},{2},{3},{4},{5},{6}",
                    bar, time, level.Price, level.Bid, level.Ask, level.Volume, level.Ticks));
            }
        }

        private void Flush()
        {
            try
            {
                if (_bars != null) _bars.Flush();
                if (_levels != null) _levels.Flush();
            }
            catch (Exception ex)
            {
                _error = ex.Message;
            }
        }

        private void CloseFiles()
        {
            if (_bars != null) { _bars.Dispose(); _bars = null; }
            if (_levels != null) { _levels.Dispose(); _levels = null; }
        }

        private static string SafeName(string text)
        {
            if (string.IsNullOrEmpty(text))
                return "x";

            foreach (var ch in Path.GetInvalidFileNameChars())
                text = text.Replace(ch, '_');

            return text.Replace(' ', '_');
        }
    }
}
