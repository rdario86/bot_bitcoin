import streamlit as st
import pandas as pd
import ccxt
import plotly.graph_objects as go
from datetime import timedelta
import time

st.set_page_config(page_title="App Trading: ORB Bitcoin 5m", layout="wide")

# ==========================================
# 1. PARÁMETROS DE LA ESTRATEGIA
# ==========================================
with st.sidebar.form(key='panel_ajustes'):
    st.header("⚙️ Ajustes ORB")
    dias_historial = st.slider("Días de Backtesting", 1, 45, 30)
    ratio_rr = st.number_input("Ratio Riesgo/Beneficio (1:X)", value=2.0)
    fuerza_cuerpo = st.slider("Fuerza de Ruptura (Cuerpo %)", 50, 100, 60, 5)
    max_extension = st.slider("Extensión Máx. de Entrada (%)", 0.1, 2.0, 0.5, 0.1, help="Distancia máxima permitida entre la línea de ruptura y el cierre de la vela. Si es mayor, no se opera para evitar Stop Loss gigantes.")
    
    ejecutar_btn = st.form_submit_button("Confirmar Ajustes y Ejecutar")

# ==========================================
# 2. CONEXIÓN A BINGX OPTIMIZADA
# ==========================================
@st.cache_data(ttl=300, show_spinner="Descargando datos de BingX Futuros (5m)...")
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
            try:
                velas = exchange.fetch_ohlcv('BTC/USDT:USDT', timeframe='5m', since=since, limit=limite_velas)
                if not velas:
                    break
                
                todas_las_velas.extend(velas)
                since = velas[-1][0] + 300000 
                
                if len(velas) < limite_velas:
                    break 
                
                time.sleep(0.1) 
                
            except Exception as limite_api:
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
        st.error(f"Error crítico de conexión: {e}")
        return pd.DataFrame()

# ==========================================
# 3. MOTOR DE BACKTESTING 
# ==========================================
def ejecutar_backtest(df, pct_cuerpo, ratio, max_ext):
    operaciones = []
    if df.empty:
        return pd.DataFrame(operaciones)
        
    fechas = df['Date'].unique()
    
    for fecha in fechas:
        df_dia = df[df['Date'] == fecha]
        
        vela_apertura = df_dia.between_time('09:30', '09:30')
        if vela_apertura.empty:
            continue
            
        max_5min = vela_apertura['High'].iloc[0]
        min_5min = vela_apertura['Low'].iloc[0]
        
        mitad_rango = (max_5min + min_5min) / 2
        
        horario_operativo = df_dia.between_time('09:35', '10:00')
        trade_registrado = False
        
        for idx, row in horario_operativo.iterrows():
            if trade_registrado: break 
            
            tamaño_vela = row['High'] - row['Low']
            if tamaño_vela == 0: continue
                
            tamaño_cuerpo = abs(row['Open'] - row['Close'])
            entrada = row['Close']
            
            distancia_long_pct = ((entrada - max_5min) / max_5min) * 100
            distancia_short_pct = ((min_5min - entrada) / min_5min) * 100
            
            tipo_trade = None
            
            if entrada > max_5min:
                if (tamaño_cuerpo / tamaño_vela) >= (pct_cuerpo / 100):
                    if distancia_long_pct <= max_ext:
                        tipo_trade = 'Long 🟢'
                        stop_loss = mitad_rango 
                        take_profit = entrada + ((entrada - stop_loss) * ratio)
                    
            elif entrada < min_5min:
                if (tamaño_cuerpo / tamaño_vela) >= (pct_cuerpo / 100):
                    if distancia_short_pct <= max_ext:
                        tipo_trade = 'Short 🔴'
                        stop_loss = mitad_rango 
                        take_profit = entrada - ((stop_loss - entrada) * ratio)
            
            if tipo_trade:
                trade_registrado = True
                resultado = "Sin Resolución ⏳"
                
                df_post_entrada = df_dia.loc[idx:]
                for jdx, vela in df_post_entrada.iterrows():
                    if jdx == idx: continue 
                    
                    if "Long" in tipo_trade:
                        if vela['Low'] <= stop_loss:
                            resultado = "Pérdida ❌"
                            break
                        elif vela['High'] >= take_profit:
                            resultado = "Ganancia ✅"
                            break
                    else: 
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
                    'Max_ORB': max_5min, 'Min_ORB': min_5min, 'Mitad_ORB': mitad_rango
                })

    return pd.DataFrame(operaciones)

# ==========================================
# 4. INTERFAZ Y RESULTADOS
# ==========================================
st.title("📈 App de Estrategia ORB - Bitcoin (5 Minutos)")

df_btc = obtener_datos_bingx(dias_historial)
df_operaciones = ejecutar_backtest(df_btc, fuerza_cuerpo, ratio_rr, max_extension)

if df_btc.empty:
    st.warning("No se pudieron cargar los datos.")
elif df_operaciones.empty:
    st.info("No se encontraron operaciones. El filtro de extensión máxima o la fuerza del cuerpo podrían estar muy estrictos.")
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
        
        df_dia = df_btc.loc[f"{dia_str} 09:00:00":f"{dia_str} 11:30:00"]
        
        fig = go.Figure(data=[go.Candlestick(
            x=df_dia.index,
            open=df_dia['Open'], high=df_dia['High'],
            low=df_dia['Low'], close=df_dia['Close'],
            name='BTC/USDT'
        )])
        
        fig.add_hline(y=trade_data['Max_ORB'], line_dash="dash", line_color="blue", annotation_text="Max 5m (09:30)")
        fig.add_hline(y=trade_data['Min_ORB'], line_dash="dash", line_color="blue", annotation_text="Min 5m (09:30)")
        fig.add_hline(y=trade_data['Mitad_ORB'], line_dash="dash", line_color="yellow", annotation_text="Mitad 5m (50%)")
        
        color_flecha = "green" if "Long" in trade_data['Tipo'] else "red"
        simbolo_flecha = "triangle-up" if "Long" in trade_data['Tipo'] else "triangle-down"
        
        fig.add_trace(go.Scatter(
            x=[fecha_obj], y=[trade_data['Entrada']],
            mode='markers', name='Punto de Entrada',
            marker=dict(symbol=simbolo_flecha, size=15, color=color_flecha)
        ))
        
        fig.add_hline(y=trade_data['Stop Loss'], line_dash="solid", line_color="red", annotation_text="Stop Loss (50%)")
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
