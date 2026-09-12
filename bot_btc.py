import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta

st.set_page_config(page_title="App Trading: ORB Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA (SIDEBAR CON BOTÓN)
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB")
    dias_historial = st.slider("Días de Backtesting", 1, 30, 7, help="Cantidad de días hacia atrás a descargar.")
    riesgo_porcentaje = st.slider("Riesgo por Operación (%)", 1.0, 3.0, 1.0, 0.5)
    ratio_rr = st.number_input("Ratio Riesgo/Beneficio (1:X)", value=2.0)
    fuerza_rechazo = st.slider("Rechazo Mínimo de Mecha (%)", 30, 80, 50, 5)
    
    # Este botón detiene la recarga automática hasta ser presionado
    ejecutar_btn = st.form_submit_button("Confirmar Ajustes y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX Y DESCARGA DE DATOS
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de BingX Futuros...")
def obtener_datos_bingx(dias):
    try:
        exchange = ccxt.bingx({
            'enableRateLimit': True,
            # En CCXT, los futuros perpetuos de BingX se manejan como 'swap'
            'options': {'defaultType': 'swap'}
        })
        
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        todas_las_velas = []
        limite_velas = 1000 # Límite de seguridad ajustado para BingX
        
        while True:
            # Símbolo unificado de CCXT para futuros lineales (margen en USDT)
            velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='1m', since=since, limit=limite_velas)
            if not velas:
                break
            todas_las_velas.extend(velas)
            since = velas[-1][0] + 60000 
            if len(velas) < limite_velas:
                break 
                
        if not todas_las_velas:
            return pd.DataFrame()
            
        df = pd.DataFrame(todas_las_velas, columns=['Timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['Timestamp'] = pd.to_datetime(df['Timestamp'], unit='ms')
        df.set_index('Timestamp', inplace=True)
        
        df.index = df.index.tz_localize('UTC').tz_convert('America/New_York')
        df = df[~df.index.duplicated(keep='first')]
        
        df['Date'] = df.index.date
        df['Typical_Price'] = (df['High'] + df['Low'] + df['Close']) / 3
        df['Vol_x_TP'] = df['Typical_Price'] * df['Volume']
        df['Cum_Vol'] = df.groupby('Date')['Volume'].cumsum()
        df['Cum_Vol_x_TP'] = df.groupby('Date')['Vol_x_TP'].cumsum()
        df['VWAP'] = df['Cum_Vol_x_TP'] / df['Cum_Vol']
        
        return df
    
    except Exception as e:
        st.error(f"Error de conexión con BingX: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (LÓGICA ORB)
# ==========================================
def ejecutar_backtest(df, pct_rechazo, ratio):
    operaciones = []
    if df.empty:
        return pd.DataFrame(operaciones)
        
    fechas = df['Date'].unique()
    
    for fecha in fechas:
        df_dia = df[df['Date'] == fecha]
        
        rango_inicial = df_dia.between_time('09:30', '09:34')
        if rango_inicial.empty or len(rango_inicial) < 5:
            continue
            
        max_5min = rango_inicial['High'].max()
        min_5min = rango_inicial['Low'].min()
        
        horario_operativo = df_dia.between_time('09:35', '10:00')
        trade_abierto = False
        
        for idx, row in horario_operativo.iterrows():
            if trade_abierto: break 
            
            tamaño_vela = row['High'] - row['Low']
            if tamaño_vela == 0: continue
                
            cuerpo_max = max(row['Open'], row['Close'])
            cuerpo_min = min(row['Open'], row['Close'])
            mecha_inf = cuerpo_min - row['Low']
            mecha_sup = row['High'] - cuerpo_max
            
            if row['Close'] > max_5min and row['Close'] > row['VWAP']:
                if (mecha_inf / tamaño_vela) >= (pct_rechazo / 100):
                    entrada = row['High']
                    stop_loss = row['Low'] 
                    take_profit = entrada + ((entrada - stop_loss) * ratio)
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': 'Long 🟢', 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Max_ORB': max_5min, 'Min_ORB': min_5min
                    })
                    trade_abierto = True
                    
            elif row['Close'] < min_5min and row['Close'] < row['VWAP']:
                if (mecha_sup / tamaño_vela) >= (pct_rechazo / 100):
                    entrada = row['Low']
                    stop_loss = row['High'] 
                    take_profit = entrada - ((stop_loss - entrada) * ratio)
                    
                    operaciones.append({
                        'Fecha': idx, 'Tipo': 'Short 🔴', 'Entrada': entrada,
                        'Stop Loss': stop_loss, 'Take Profit': take_profit,
                        'Max_ORB': max_5min, 'Min_ORB': min_5min
                    })
                    trade_abierto = True

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ DE USUARIO Y GRÁFICOS
# ==========================================
st.title("📈 App de Estrategia ORB - Bitcoin")
st.markdown("Analiza la ruptura del rango de 5 minutos en la apertura de Nueva York con datos reales de futuros (BingX).")

# Flujo de ejecución principal
df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, fuerza_rechazo, ratio_rr)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos. Verifica que los servidores de BingX estén respondiendo.")
elif df_operaciones.empty:
    st.info(f"No se encontraron operaciones en los últimos {dias_historial} días con estos parámetros. Intenta reducir la exigencia del rechazo.")
else:
    col1, col2, col3 = st.columns(3)
    col1.metric("Operaciones Encontradas", len(df_operaciones))
    col2.metric("Temporalidad Evaluada", "1 Minuto")
    col3.metric("Datos Analizados", f"{len(df_btc)} velas")
    
    st.subheader("📋 Registro de Operaciones")
    st.dataframe(df_operaciones[['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit']], use_container_width=True)
    
    st.divider()
    
    st.subheader("🔍 Visualizador de Operaciones")
    opciones_trades = df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S').tolist()
    trade_seleccionado = st.selectbox("Selecciona la fecha del Trade:", opciones_trades)
    
    if trade_seleccionado:
        trade_data = df_operaciones[df_operaciones['Fecha'].dt.strftime('%Y-%m-%d %H:%M:%S') == trade_seleccionado].iloc[0]
        fecha_obj = trade_data['Fecha']
        dia_str = fecha_obj.strftime('%Y-%m-%d')
        
        df_dia = df_btc.loc[f"{dia_str} 09:00:00":f"{dia_str} 11:00:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='BTC/USDT'
        )])
        
        fig.add_trace(go.Scatter(x=df_dia.index, y=df_dia['VWAP'], mode='lines', name='VWAP', line=dict(color='orange', width=1.5)))
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Max 5m (09:30)")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Min 5m (09:30)")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Punto de Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss")
        fig.add_hline(y=trade_data['Take Profit'], line_dash="solid", line_color="green", annotation_text="Take Profit")
        
        fig.update_layout(
            title=f"Trade {trade_data['Tipo']} el {dia_str}",
            yaxis_title="Precio (USD)",
            xaxis_title="Hora (EST - NY)",
            height=600,
            xaxis_rangeslider_visible=False,
            template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
