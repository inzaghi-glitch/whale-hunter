import asyncio, logging, time
from datetime import datetime
from typing import Optional
import aiohttp
from web3 import Web3

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("whale_hunter")

TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID   = "YOUR_CHAT_ID"
ALCHEMY_RPC = None
WATCHED_WALLETS = []
WHALE_THRESHOLD_USD = 50_000

RPC_ENDPOINTS = [url for url in [ALCHEMY_RPC,"https://eth.llamarpc.com","https://rpc.ankr.com/eth","https://cloudflare-eth.com","https://ethereum.publicnode.com"] if url]
POLL_INTERVAL_SEC = 12
DEX_ROUTERS = {"0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D":"Uniswap V2","0xE592427A0AEce92De3Edee1F18E0157C05861564":"Uniswap V3","0xd9e1cE17f2641f24aE83637ab66a2cca9C378B9F":"SushiSwap","0x1111111254EEB25477B68fb85Ed929f73A960582":"1inch V5","0xDef1C0ded9bec7F1a1670819833240f027b25EfF":"0x Exchange","0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD":"Uniswap Universal"}
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
KNOWN_TOKENS = {"0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48":("USDC",6),"0xdAC17F958D2ee523a2206206994597C13D831ec7":("USDT",6),"0x6B175474E89094C44Da98b954EedeAC495271d0F":("DAI",18),"0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599":("WBTC",8),"0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2":("WETH",18),"0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984":("UNI",18),"0x514910771AF9Ca656af840dff83E8264EcF986CA":("LINK",18),"0x7D1AfA7B718fb893dB30A3aBc0Cfc608AaCfeBB0":("MATIC",18),"0x95aD61b0a150d79219dCF64E1E6Cc01f0B64C4cE":("SHIB",18),"0x4d224452801ACEd8B2F0aebE155379bb5D594381":("APE",18)}
APPROX_PRICES_USD = {"USDC":1.0,"USDT":1.0,"DAI":1.0,"WBTC":65000,"WETH":3200,"ETH":3200,"UNI":10,"LINK":15,"MATIC":0.9,"SHIB":0.000025,"APE":1.5}

def get_web3():
    for url in RPC_ENDPOINTS:
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout":10}))
            if w3.is_connected(): log.info(f"Connected: {url}"); return w3
        except: continue
    raise ConnectionError("All RPCs failed.")

async def send_telegram(session, message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        async with session.post(url, json={"chat_id":TELEGRAM_CHAT_ID,"text":message,"parse_mode":"HTML","disable_web_page_preview":True}, timeout=aiohttp.ClientTimeout(total=10)) as r:
            if r.status != 200: log.warning(f"Telegram error: {await r.text()}")
    except Exception as e: log.error(f"Telegram failed: {e}")

def estimate_usd(s,a): return a * APPROX_PRICES_USD.get(s,0)
def short_addr(a): return f"{a[:6]}...{a[-4:]}"
def etherscan(a,k="address"): return f"https://etherscan.io/{k}/{a}"
def fmt(n): return f"{n/1_000_000:.2f}M" if n>=1_000_000 else (f"{n/1_000:.1f}K" if n>=1_000 else f"{n:.2f}")

class WhaleHunter:
    def __init__(self):
        self.w3=get_web3(); self.last_block=None; self.alerted_txs=set()
    def reconnect(self): self.w3=get_web3()
    def check_eth(self,tx):
        val=float(self.w3.from_wei(tx["value"],"ether")); usd=estimate_usd("ETH",val)
        if usd>=WHALE_THRESHOLD_USD: return {"type":"ETH Transfer","hash":tx["hash"].hex(),"from":tx["from"],"to":tx["to"] or "Contract","amount":val,"symbol":"ETH","usd":usd}
    def parse_logs(self,receipt):
        alerts=[]
        for e in receipt.get("logs",[]):
            t=e.get("topics",[])
            if not t or t[0].hex()!=TRANSFER_TOPIC or len(t)<3: continue
            token=KNOWN_TOKENS.get(Web3.to_checksum_address(e["address"]))
            if not token: continue
            s,d=token; raw=int(e["data"].hex() if isinstance(e["data"],bytes) else e["data"],16); amount=raw/(10**d); usd=estimate_usd(s,amount)
            if usd>=WHALE_THRESHOLD_USD: alerts.append({"type":"ERC-20 Transfer","hash":receipt["transactionHash"].hex(),"from":Web3.to_checksum_address("0x"+t[1].hex()[-40:]),"to":Web3.to_checksum_address("0x"+t[2].hex()[-40:]),"amount":amount,"symbol":s,"usd":usd})
        return alerts
    def check_dex(self,tx,receipt):
        to=tx.get("to") or ""; dex=DEX_ROUTERS.get(Web3.to_checksum_address(to) if to else "",None)
        if not dex: return None
        for e in receipt.get("logs",[]):
            t=e.get("topics",[])
            if not t or t[0].hex()!=TRANSFER_TOPIC or len(t)<3: continue
            token=KNOWN_TOKENS.get(Web3.to_checksum_address(e["address"]))
            if not token: continue
            s,d=token; raw=int(e["data"].hex() if isinstance(e["data"],bytes) else e["data"],16); amount=raw/(10**d); usd=estimate_usd(s,amount)
            if usd>=WHALE_THRESHOLD_USD: return {"type":f"DEX Swap ({dex})","hash":receipt["transactionHash"].hex(),"from":tx["from"],"to":to,"amount":amount,"symbol":s,"usd":usd}
    def format_alert(self,d,watched=None):
        emoji="🔁" if "DEX" in d["type"] else ("👁️" if watched else ("⛓️" if d["symbol"]=="ETH" else "💸"))
        return (f"{emoji} <b>WHALE ALERT — {d['type'].upper()}</b>\n\n💰 <b>{fmt(d['amount'])} {d['symbol']}</b>  ≈  <b>${fmt(d['usd'])}</b>\n\n📤 From: <a href='{etherscan(d['from'])}'>{short_addr(d['from'])}</a>\n📥 To:   <a href='{etherscan(d['to'])}'>{short_addr(d['to'])}</a>\n\n🔗 <a href='{etherscan(d['hash'],'tx')}'>View on Etherscan</a>\n🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    async def process_block(self,session,block_number):
        try: block=self.w3.eth.get_block(block_number,full_transactions=True)
        except Exception as e: log.error(f"Block error: {e}"); return
        log.info(f"Block {block_number} — {len(block['transactions'])} txs")
        for tx in block["transactions"]:
            h=tx["hash"].hex()
            if h in self.alerted_txs: continue
            alerts=[]; watched=next((w for w in WATCHED_WALLETS if w.lower() in ((tx.get("from") or "").lower(),(tx.get("to") or "").lower())),None)
            if tx["value"]>0:
                a=self.check_eth(tx)
                if a: alerts.append(a)
            if tx.get("input","0x") not in ("0x",b"","") or watched:
                try:
                    receipt=self.w3.eth.get_transaction_receipt(h); alerts.extend(self.parse_logs(receipt))
                    swap=self.check_dex(tx,receipt)
                    if swap: alerts.append(swap)
                except: pass
            for alert in alerts:
                if h not in self.alerted_txs:
                    await send_telegram(session,self.format_alert(alert,watched)); self.alerted_txs.add(h); log.info(f"🐋 {alert['type']} ${fmt(alert['usd'])}"); break
        if len(self.alerted_txs)>5000: self.alerted_txs=set(list(self.alerted_txs)[-2000:])
    async def run(self):
        log.info("🐋 Starting...")
        async with aiohttp.ClientSession() as session:
            await send_telegram(session,f"🐋 <b>Whale Hunter Online</b>\nChain: Ethereum Mainnet\nThreshold: ${WHALE_THRESHOLD_USD:,}\nStarted: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
            while True:
                try:
                    current=self.w3.eth.block_number
                    if self.last_block is None: self.last_block=current-1
                    for b in range(self.last_block+1,current+1): await self.process_block(session,b); self.last_block=b
                except Exception as e: log.error(f"Loop error: {e}"); self.reconnect()
                await asyncio.sleep(POLL_INTERVAL_SEC)

import asyncio
asyncio.run(WhaleHunter().run())
