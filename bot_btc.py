import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta

st.set_page_config(page_title="App Trading: ORB Bitcoin", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB (Sin Indicadores)")
    dias_historial = st.slider("Días de Backtesting", 1, 30, 7)
    riesgo_porcentaje = st.slider("Riesgo por Operación (%)", 1.0, 3.0, 1.0, 0.5)
    ratio_rr = st.number_input("Ratio Riesgo/Beneficio (1:X)", value=2.0)
    fuerza_rechazo = st.slider("Rechazo Mínimo de Mecha (%)", 30, 80, 50, 5)
    ejecutar_btn = st.form_submit_button("Confirmar Ajustes y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX (SIN VWAP)
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de BingX Futuros...")
def obtener_datos_bingx(dias):
    try:
        exchange = ccxt.bingx({
            'enableRateLimit': True,
            'options': {'defaultType': 'swap'}
        })
        
        ahora = pd.Timestamp.utcnow()
        inicio = ahora - pd.Timedelta(days=dias)
        since = exchange.parse8601(inicio.isoformat())
        
        todas_las_velas = []
        limite_velas = 1000 
        
        while True:
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
        
        return df
    
    except Exception as e:
        st.error(f"Error de conexión con BingX: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING (PURA ACCIÓN DEL PRECIO)
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
        trade_registrado = False
        
        for idx, row in horario_operativo.iterrows():
            if trade_registrado: break 
            
            tamaño_vela = row['High'] - row['Low']
            if tamaño_vela == 0: continue
                
            cuerpo_max = max(row['Open'], row['Close'])
            cuerpo_min = min(row['Open'], row['Close'])
            mecha_inf = cuerpo_min - row['Low']
            mecha_sup = row['High'] - cuerpo_max
            
            tipo_trade = None
            
            # Condición LONG (Ruptura del máximo de 5 min)
            if row['Close'] > max_5min:
                if (mecha_inf / tamaño_vela) >= (pct_rechazo / 100):
                    tipo_trade = 'Long 🟢'
                    entrada = row['High']
                    stop_loss = row['Low'] 
                    take_profit = entrada + ((entrada - stop_loss) * ratio)
                    
            # Condición SHORT (Ruptura del mínimo de 5 min)
            elif row['Close'] < min_5min:
                if (mecha_sup / tamaño_vela) >= (pct_rechazo / 100):
                    tipo_trade = 'Short 🔴'
                    entrada = row['Low']
                    stop_loss = row['High'] 
                    take_profit = entrada - ((stop_loss - entrada) * ratio)
            
            if tipo_trade:
                trade_registrado = True
                resultado = "Sin Resolución ⏳"
                
                # Simular evolución del precio
                df_post_entrada = df_dia.loc[idx:]
                for jdx, vela in df_post_entrada.iterrows():
                    if "Long" in tipo_trade:
                        if vela['Low'] <= stop_loss:
                            resultado = "Pérdida ❌"
                            break
                        elif vela['High'] >= take_profit:
                            resultado = "Ganancia ✅"
                            break
                    else: # Short
                        if vela['High'] >= stop_loss:
                            resultado = "Pérdida ❌"
                            break
                        elif vela['Low'] <= take_profit:
                            resultado = "Ganancia ✅"
                            break
                
                operaciones.append({
                    'Fecha': idx, 'Tipo': tipo_trade, 'Entrada': entrada,
                    'Stop Loss': stop_loss, 'Take Profit': take_profit,
                    'Resultado': resultado,
                    'Max_ORB': max_5min, 'Min_ORB': min_5min
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 App de Estrategia ORB - Bitcoin")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, fuerza_rechazo, ratio_rr)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones con estos parámetros.")
else:
    total_trades = len(df_operaciones)
    aciertos = len(df_operaciones[df_operaciones['Resultado'] == "Ganancia ✅"])
    fallos = len(df_operaciones[df_operaciones['Resultado'] == "Pérdida ❌"])
    win_rate = (aciertos / total_trades) * 100 if total_trades > 0 else 0
    
    st.subheader("📊 Resumen de Rendimiento")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Total Operaciones", total_trades)
    col2.metric("Aciertos ✅", aciertos)
    col3.metric("Fallos ❌", fallos)
    col4.metric("% Win Rate", f"{win_rate:.1f}%")
    
    st.divider()
    
    st.subheader("📋 Registro Detallado")
    df_mostrar = df_operaciones.copy()
    columnas_moneda = ['Entrada', 'Stop Loss', 'Take Profit']
    
    for col in columnas_moneda:
        df_mostrar[col] = df_mostrar[col].apply(lambda x: f"${x:,.2f}")
    
    st.dataframe(df_mostrar[['Fecha', 'Tipo', 'Entrada', 'Stop Loss', 'Take Profit', 'Resultado']], use_container_width=True)
    
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
            title=f"Trade {trade_data['Tipo']} el {dia_str} | Resultado: {trade_data['Resultado']}",
            yaxis_title="Precio (USD)",
            xaxis_title="Hora (EST - NY)",
            height=600,
            xaxis_rangeslider_visible=False,
            template="plotly_dark"
        )
        
        st.plotly_chart(fig, use_container_width=True)
