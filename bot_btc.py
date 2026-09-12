import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta

st.set_page_config(page_title="App Trading: ORB Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA (SIDEBAR)
# ==========================================
st.sidebar.title("⚙️ Parámetros ORB")
dias_historial = st.sidebar.slider("Días de Backtesting", 1, 30, 7, help="Cantidad de días hacia atrás a descargar de Binance.")
riesgo_porcentaje = st.sidebar.slider("Riesgo por Operación (%)", 1.0, 3.0, 1.0, 0.5)
ratio_rr = st.sidebar.number_input("Ratio Riesgo/Beneficio (1:X)", value=2.0)
fuerza_rechazo = st.sidebar.slider("Rechazo Mínimo de Mecha (%)", 30, 80, 50, 5, help="Porcentaje de la vela que debe ser mecha para confirmar el rechazo.")

# ==========================================
# 2. CONEXIÓN A BINANCE Y DESCARGA DE DATOS
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de Binance Futuros...")
def obtener_datos_binance(dias):
    exchange = ccxt.binance({
        'enableRateLimit': True,
        'options': {'defaultType': 'future'}
    })
    
    # Calcular desde cuándo descargar
    ahora = pd.Timestamp.utcnow()
    inicio = ahora - pd.Timedelta(days=dias)
    since = exchange.parse8601(inicio.isoformat())
    
    todas_las_velas = []
    
    # Bucle para descargar más de las 1500 velas permitidas por llamada
    while True:
        velas = exchange.fetch_ohlcv('BTC/USDT', timeframe='1m', since=since, limit=1500)
        if not velas:
            break
        todas_las_velas.extend(velas)
        since = velas[-1][0] + 60000 # Avanzar 1 minuto
        if len(velas) < 1500:
            break # Ya descargamos todo
            
    if not todas_las_velas:
        return pd.DataFrame()
        
    df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
    df.set_index('Timestamp', inplace=True)
    
    # Convertir a hora de Nueva York (EST)
    df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
    
    # Eliminar duplicados si los hay
    df = df[~df.index.duplicated(keep='first')]
    
    # Cálculo del VWAP diario
    df['Date'] = df.index.date
    df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
    df['Vol_x_TP'] = df['Typical_Price'] * df['Volume']
    df['Cum_Vol'] = df.groupby('Date')['Volume'].cumsum()
    df['Cum_Vol_x_TP'] = df.groupby('Date')['Vol_x_TP'].cumsum()
    df['VWAP'] = df['Cum_Vol_x_TP'] / df['Cum_Vol']
    
    return df

df_btc = obtener_datos_binance(dias_historial)

# ==========================================
# 3. MOTOR DE BACKTESTING (LÓGICA ORB)
# ==========================================
def ejecutar_backtest(df, pct_rechazo, ratio):
    operaciones = []
    fechas = df['Date'].unique()
    
    for fecha in fechas:
        df_dia = df[df['Date'] == fecha]
        
        # 1. Identificar rango inicial (09:30 a 09:34 incluye las 5 velas de 1m)
        rango_inicial = df_dia.between_time('09:30', '09:34')
        if rango_inicial.empty or len(rango_inicial) < 5:
            continue
            
        max_5min = rango_inicial['High'].max()
        min_5min = rango_inicial['Low'].min()
        
        # 2. Buscar entrada (09:35 a 10:00)
        horario_operativo = df_dia.between_time('09:35', '10:00')
        trade_abierto = False
        
        for idx, row in horario_operativo.iterrows():
            if trade_abierto: break # Solo un trade al día para no sobreoperar
            
            tamaño_vela = row['High'] - row['Low']
            if tamaño_vela == 0: continue
                
            cuerpo_max = max(row['Open'], row['Close'])
            cuerpo_min = min(row['Open'], row['Close'])
            mecha_inf = cuerpo_min - row['Low']
            mecha_sup = row['High'] - cuerpo_max
            
            # Condición LONG (Compra)
            if row['Close'] > max_5min and row['Close'] > row['VWAP']:
                if (mecha_inf / tamaño_vela) >= (pct_rechazo / 100):
                    entrada = row['High']
                    stop_loss = row['Low'] # SL debajo del retroceso
                    take_profit = entrada + ((entrada - stop_loss) * ratio)
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': 'Long 🟢', 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Max_ORB': max_5min, 'Min_ORB': min_5min
                    })
                    trade_abierto = True
                    
            # Condición SHORT (Venta)
            elif row['Close'] < min_5min and row['Close'] < row['VWAP']:
                if (mecha_sup / tamaño_vela) >= (pct_rechazo / 100):
                    entrada = row['Low']
                    stop_loss = row['High'] # SL arriba del retroceso
                    take_profit = entrada - ((stop_loss - entrada) * ratio)
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': 'Short 🔴', 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Max_ORB': max_5min, 'Min_ORB': min_5min
                    })
                    trade_abierto = True

    return pd.DataFrame(operaciones)

df_operaciones = ejecutar_backtest(df_btc, fuerza_rechazo, ratio_rr)

# ==========================================
# 4. INTERFAZ DE USUARIO Y GRÁFICOS
# ==========================================
st.title("📈 App de Estrategia ORB - Bitcoin")
st.markdown("Analiza la ruptura del rango de 5 minutos en la apertura de Nueva York con datos reales de futuros de Binance.")

if df_operaciones.empty:
    st.warning(f"No se encontraron operaciones en los últimos {dias_historial} días con estos parámetros. Intenta reducir la exigencia del rechazo.")
else:
    # Métricas Generales
    col1, col2, col3 = st.columns(3)
    col1.metric("Operaciones Encontradas", len(df_operaciones))
    col2.metric("Temporalidad Evaluada", "1 Minuto")
    col3.metric("Datos Analizados", f"{len(df_btc)} velas")
    
    st.subheader("📋 Registro de Operaciones")
    st.dataframe(df_operaciones[['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit']], use_container_width=True)
    
    st.divider()
    
    # Selector para el gráfico interactivo
    st.subheader("🔍 Visualizador de Operaciones")
    st.write("Selecciona una operación de la lista para verla en el gráfico interactivo con sus niveles exactos.")
    
    opciones_trades = df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
    
    if trade_seleccionado:
        # Filtrar datos de la operación seleccionada
        trade_data = df_operaciones[df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Fecha']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        # Filtrar el DataFrame principal solo para ese día, de 09:00 a 11:00 EST para tener contexto visual
        df_dia = df_btc.loc[f"{dia_str} 09:00:00":f"{dia_str} 11:00:00"]
        
        # Crear gráfico de velas con Plotly
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='BTC/USDT'
        )])
        
        # Agregar VWAP
        fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['VWAP'], mode='lines', name='VWAP', line=dict(color='orange', width=1.5)))
        
        # Dibujar líneas del Rango Inicial (ORB 5 min)
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Max 5m (09:30)")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Min 5m (09:30)")
        
        # Dibujar niveles de la operación (Entrada, SL, TP)
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        # Marcar la vela exacta de entrada
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Punto de Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        # Líneas de Stop Loss y Take Profit
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        # Configuración estética del gráfico
        fig.update_layout(
            title=f"Trade {trade_data['Tipo']} el {dia_str}",
            yaxis_title="Precio (USD)",
            xaxis_title="Hora (EST - NY)",
            height=600,
            xaxis_rangeslider_visible=False,
            template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)