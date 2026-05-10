#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║        deploy_flash_arb.py — ArbPrime Deployment Script      ║
║        Compiles FlashLoanArb.sol → Deploys to Polygon        ║
║        Run once. Save the contract address to .env.          ║
╚══════════════════════════════════════════════════════════════╝

Usage:
    pip install web3 py-solc-x python-dotenv
    python deploy_flash_arb.py

Required .env:
    POLY_PRIVATE_KEY=0x...
    POLY_RPC_URL=https://polygon-rpc.com   (or Alchemy/Infura)

Output:
    Prints contract address → paste into .env as FLASH_ARB_CONTRACT
"""

import os
import sys
import json
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

try:
    from web3 import Web3
    from solcx import compile_source, install_solc, get_installed_solc_versions
except ImportError:
    print("Missing deps. Run: pip install web3 py-solc-x python-dotenv")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────
PRIVATE_KEY = os.getenv("POLY_PRIVATE_KEY", "")
RPC_URL     = os.getenv("POLY_RPC_URL", "https://polygon-rpc.com")
SOL_FILE    = Path(__file__).parent / "contracts" / "FlashLoanArb.sol"
SOLC_VER    = "0.8.20"

# ── Validate ──────────────────────────────────────────────────────────────
if not PRIVATE_KEY:
    print("ERROR: POLY_PRIVATE_KEY not set in .env")
    sys.exit(1)

if not SOL_FILE.exists():
    print(f"ERROR: Contract not found at {SOL_FILE}")
    sys.exit(1)

# ── Connect ───────────────────────────────────────────────────────────────
print(f"Connecting to Polygon RPC: {RPC_URL}")
w3 = Web3(Web3.HTTPProvider(RPC_URL))

if not w3.is_connected():
    print("ERROR: Cannot connect to Polygon RPC. Check POLY_RPC_URL.")
    sys.exit(1)

account = w3.eth.account.from_key(PRIVATE_KEY)
deployer = account.address
print(f"Deployer wallet : {deployer}")

# ── MATIC balance check ───────────────────────────────────────────────────
balance_wei  = w3.eth.get_balance(deployer)
balance_matic = w3.from_wei(balance_wei, "ether")
print(f"MATIC balance   : {balance_matic:.4f} MATIC")

if balance_matic < 0.5:
    print(f"\nWARNING: Low MATIC balance ({balance_matic:.4f}).")
    print("Need at least ~0.5 MATIC to deploy. Get some from:")
    print("  Faucet : https://www.alchemy.com/faucets/polygon-mainnet (0.5/day free)")
    print("  Wallet : 0x369c2DDDBEb910c48356910069B2903b3Cb4d535")
    if balance_matic < 0.05:
        print("ERROR: Not enough MATIC to proceed. Exiting.")
        sys.exit(1)
    print("Proceeding anyway (low balance, may fail)...\n")

# ── Install solc if needed ────────────────────────────────────────────────
installed = [str(v) for v in get_installed_solc_versions()]
if SOLC_VER not in installed:
    print(f"Installing solc {SOLC_VER}...")
    install_solc(SOLC_VER)
    print("solc installed.")

# ── Read + Compile ────────────────────────────────────────────────────────
print(f"\nCompiling {SOL_FILE.name}...")
source = SOL_FILE.read_text()

compiled = compile_source(
    source,
    output_values=["abi", "bin"],
    solc_version=SOLC_VER,
    optimize=True,
    optimize_runs=200,
)

# Extract the FlashLoanArb contract
contract_id = None
for key in compiled:
    if "FlashLoanArb" in key:
        contract_id = key
        break

if not contract_id:
    print("ERROR: FlashLoanArb contract not found in compiled output.")
    sys.exit(1)

contract_interface = compiled[contract_id]
abi      = contract_interface["abi"]
bytecode = contract_interface["bin"]

print(f"Compiled OK — bytecode: {len(bytecode)//2} bytes")

# Save ABI to file (arb_prime.py loads this)
abi_path = Path(__file__).parent / "contracts" / "FlashLoanArb.abi.json"
abi_path.write_text(json.dumps(abi, indent=2))
print(f"ABI saved → {abi_path}")

# ── Estimate gas ──────────────────────────────────────────────────────────
FlashLoanArb = w3.eth.contract(abi=abi, bytecode=bytecode)
gas_estimate = FlashLoanArb.constructor().estimate_gas({"from": deployer})
gas_price    = w3.eth.gas_price
gas_cost_wei = gas_estimate * gas_price
gas_cost_matic = w3.from_wei(gas_cost_wei, "ether")

print(f"\nGas estimate    : {gas_estimate:,} units")
print(f"Gas price       : {w3.from_wei(gas_price, 'gwei'):.2f} Gwei")
print(f"Deploy cost     : ~{gas_cost_matic:.4f} MATIC")

# ── Deploy ────────────────────────────────────────────────────────────────
print("\nDeploying FlashLoanArb to Polygon mainnet...")

nonce = w3.eth.get_transaction_count(deployer)

tx = FlashLoanArb.constructor().build_transaction({
    "from":     deployer,
    "nonce":    nonce,
    "gas":      int(gas_estimate * 1.2),   # 20% buffer
    "gasPrice": int(gas_price * 1.1),      # 10% priority bump
})

signed_tx  = w3.eth.account.sign_transaction(tx, PRIVATE_KEY)
tx_hash    = w3.eth.send_raw_transaction(signed_tx.raw_transaction)

print(f"TX sent         : {tx_hash.hex()}")
print(f"Polygonscan     : https://polygonscan.com/tx/{tx_hash.hex()}")
print("Waiting for confirmation...")

# Poll for receipt (timeout 120s)
for attempt in range(24):
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt:
            break
    except Exception:
        pass
    time.sleep(5)
    print(f"  ... waiting ({(attempt+1)*5}s)")
else:
    print("WARNING: Timed out waiting for receipt. Check Polygonscan manually.")
    sys.exit(1)

if receipt["status"] != 1:
    print(f"ERROR: Transaction FAILED. Receipt: {receipt}")
    sys.exit(1)

contract_address = receipt["contractAddress"]

print(f"\n{'='*60}")
print(f"  DEPLOYMENT SUCCESSFUL")
print(f"{'='*60}")
print(f"  Contract address : {contract_address}")
print(f"  Block            : {receipt['blockNumber']}")
print(f"  Gas used         : {receipt['gasUsed']:,}")
print(f"  Polygonscan      : https://polygonscan.com/address/{contract_address}")
print(f"{'='*60}")
print(f"\nNext step — add to your .env:")
print(f"  FLASH_ARB_CONTRACT={contract_address}")
print(f"\nThen set SIMULATE_MODE=false and run arb_prime.py")
