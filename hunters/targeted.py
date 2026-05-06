"""
hunters/targeted.py — V7.0 Targeted Sentinel
Evaluates transactions strictly against the targets.json watchlist.
"""
from typing import Optional
from core.types import Transaction, Signal, SignalCategory, HunterID
from core.config import Config

class TargetedSentinel:
    hunter_id = HunterID.TARGETED

    def __init__(self, config: Config):
        self.targets = getattr(config, "targets", {})

    def evaluate(self, tx: Transaction) -> Optional[Signal]:
        f_addr = tx.from_addr.lower()
        t_addr = tx.to_addr.lower()
        
        alias = self.targets.get(f_addr) or self.targets.get(t_addr)
        if not alias:
            return None
            
        direction = "OUT" if self.targets.get(f_addr) else "IN"
        
        if tx.value_eth <= 0:
            if tx.input_data and len(tx.input_data) >= 10:
                sig_prefix = tx.input_data[:10].lower()
                if sig_prefix in ("0x095ea7b3", "0x23b872dd", "0xa9059cbb"):
                    details = f"Swap/Token Transfer Attempt ({sig_prefix})"
                else:
                    details = f"Contract Interaction ({sig_prefix})"
            else:
                details = "0 ETH Ping / Data-less Interaction"
        else:
            details = f"Transferred {tx.value_eth:,.4f} ETH"

        return Signal(
            category=SignalCategory.TARGET_HIT,
            hunter_id=self.hunter_id,
            score=100,  # 100% conviction because it's a direct hit
            transaction=tx,
            label=f"[{direction}] {alias}",
            details=f"Target {alias} active. {details} | Hash: {tx.tx_hash[:10]}...",
            is_vip=True, # Ensure max visibility and telegram alert
            target_alias=alias
        )
