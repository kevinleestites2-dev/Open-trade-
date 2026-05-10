// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * ╔══════════════════════════════════════════════════════════════╗
 * ║      FLASH LOAN ARB v2 — ArbPrime Execution Engine          ║
 * ║      Aave V3 Flash Loan → DEX Arb → Profit                  ║
 * ║      Polygon Mainnet                                         ║
 * ╚══════════════════════════════════════════════════════════════╝
 *
 * Flow:
 *   1. Bot detects price gap (token priced differently on DEX X vs DEX Y)
 *   2. Bot calls execute() with: token, amount, intermediate, buyDex, sellDex, v3Fee, minProfit
 *   3. Aave lends `amount` of `token` (0.09% fee)
 *   4. Swap token → intermediate on buyDex (buy cheap side)
 *   5. Swap intermediate → token on sellDex (sell expensive side)
 *   6. Repay Aave loan + 0.09% fee
 *   7. Profit stays in contract → owner calls withdraw()
 *
 * Example (WPOL/USDC arb):
 *   token=WPOL, intermediate=USDC
 *   buyDex=SushiSwap (WPOL cheap here → buy USDC cheap)
 *   sellDex=QuickSwap V3 (WPOL expensive here → sell USDC for more WPOL)
 *   Net: end up with more WPOL than we started
 *
 * DEX IDs:
 *   0 = Uniswap V3
 *   1 = QuickSwap V2
 *   2 = SushiSwap
 *   3 = QuickSwap V3
 */

interface IERC20 {
    function approve(address spender, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

interface IPool {
    function flashLoanSimple(
        address receiverAddress,
        address asset,
        uint256 amount,
        bytes calldata params,
        uint16 referralCode
    ) external;
}

// Uniswap V3 / QuickSwap V3
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24  fee;
        address recipient;
        uint256 deadline;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    function exactInputSingle(ExactInputSingleParams calldata params)
        external returns (uint256 amountOut);
}

// Uniswap V2 style (QuickSwap V2, SushiSwap)
interface IUniswapV2Router {
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        address[] calldata path,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
}

contract FlashLoanArb {

    // ── Aave V3 Pool on Polygon ──────────────────────────────────────────
    address public constant AAVE_POOL = 0x794a61358D6845594F94dc1DB02A252b5b4814aD;

    // ── DEX Routers on Polygon ───────────────────────────────────────────
    address public constant UNISWAP_V3_ROUTER  = 0xE592427A0AEce92De3Edee1F18E0157C05861564;
    address public constant QUICKSWAP_V2_ROUTER = 0xa5E0829CaCEd8fFDD4De3c43696c57F7D7A678ff;
    address public constant SUSHISWAP_ROUTER    = 0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506;
    address public constant QUICKSWAP_V3_ROUTER = 0xf5b509bB0909a69B1c207E495f687a596C168E12;

    // ── DEX IDs ──────────────────────────────────────────────────────────
    uint8 public constant DEX_UNISWAP_V3   = 0;
    uint8 public constant DEX_QUICKSWAP_V2 = 1;
    uint8 public constant DEX_SUSHISWAP    = 2;
    uint8 public constant DEX_QUICKSWAP_V3 = 3;

    // ── Slippage: 0.5% tolerance ─────────────────────────────────────────
    uint256 public constant SLIPPAGE_BPS = 50;

    address public immutable owner;

    event ArbExecuted(
        address indexed tokenIn,
        address indexed tokenMid,
        uint256 loanAmount,
        uint256 profit,
        uint8   buyDex,
        uint8   sellDex
    );

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    constructor() {
        owner = msg.sender;
    }

    /**
     * @notice Entry point — bot calls this when arb is detected.
     * @param token         Token to borrow + profit in (e.g. WPOL)
     * @param amount        Flash loan size in token base units
     * @param intermediate  Pivot token (e.g. USDC — token→mid→token loop)
     * @param buyDex        DEX to buy intermediate (token is cheap here)
     * @param sellDex       DEX to sell intermediate (token is expensive here)
     * @param v3Fee         V3 fee tier: 3000=0.3%, 500=0.05%, 100=0.01%
     * @param minProfit     Min profit in token units — tx reverts if not met
     */
    function execute(
        address token,
        uint256 amount,
        address intermediate,
        uint8   buyDex,
        uint8   sellDex,
        uint24  v3Fee,
        uint256 minProfit
    ) external onlyOwner {
        bytes memory params = abi.encode(intermediate, buyDex, sellDex, v3Fee, minProfit);
        IPool(AAVE_POOL).flashLoanSimple(
            address(this),
            token,
            amount,
            params,
            0
        );
    }

    /**
     * @notice Aave callback — arb logic runs here with borrowed funds.
     */
    function executeOperation(
        address asset,
        uint256 amount,
        uint256 premium,
        address /* initiator */,
        bytes calldata params
    ) external returns (bool) {
        require(msg.sender == AAVE_POOL, "Only Aave pool");

        (address intermediate, uint8 buyDex, uint8 sellDex, uint24 v3Fee, uint256 minProfit) =
            abi.decode(params, (address, uint8, uint8, uint24, uint256));

        uint256 amountOwed = amount + premium;

        // Step 1: token → intermediate on buyDex (cheapest source of intermediate)
        uint256 midReceived = _swap(asset, intermediate, amount, buyDex, v3Fee);

        // Step 2: intermediate → token on sellDex (most expensive buyer of intermediate)
        uint256 tokenReceived = _swap(intermediate, asset, midReceived, sellDex, v3Fee);

        // Safety check — revert entire tx if not profitable (costs nothing, no risk)
        require(tokenReceived >= amountOwed + minProfit, "Insufficient profit");

        // Repay Aave (approve exact amount owed)
        IERC20(asset).approve(AAVE_POOL, amountOwed);

        uint256 profit = tokenReceived - amountOwed;
        emit ArbExecuted(asset, intermediate, amount, profit, buyDex, sellDex);

        return true;
    }

    /**
     * @dev Internal: dispatch swap to correct DEX router.
     */
    function _swap(
        address tokenIn,
        address tokenOut,
        uint256 amountIn,
        uint8   dex,
        uint24  v3Fee
    ) internal returns (uint256 amountOut) {
        // Minimum output with slippage protection
        uint256 minOut = amountIn * (10000 - SLIPPAGE_BPS) / 10000;

        if (dex == DEX_UNISWAP_V3 || dex == DEX_QUICKSWAP_V3) {
            address router = (dex == DEX_UNISWAP_V3) ? UNISWAP_V3_ROUTER : QUICKSWAP_V3_ROUTER;
            IERC20(tokenIn).approve(router, amountIn);
            amountOut = ISwapRouter(router).exactInputSingle(
                ISwapRouter.ExactInputSingleParams({
                    tokenIn:           tokenIn,
                    tokenOut:          tokenOut,
                    fee:               v3Fee,
                    recipient:         address(this),
                    deadline:          block.timestamp + 300,
                    amountIn:          amountIn,
                    amountOutMinimum:  minOut,
                    sqrtPriceLimitX96: 0
                })
            );
        } else {
            // V2-style: QuickSwap V2 or SushiSwap
            address router = (dex == DEX_QUICKSWAP_V2) ? QUICKSWAP_V2_ROUTER : SUSHISWAP_ROUTER;
            IERC20(tokenIn).approve(router, amountIn);
            address[] memory path = new address[](2);
            path[0] = tokenIn;
            path[1] = tokenOut;
            uint256[] memory amounts = IUniswapV2Router(router).swapExactTokensForTokens(
                amountIn,
                minOut,
                path,
                address(this),
                block.timestamp + 300
            );
            amountOut = amounts[amounts.length - 1];
        }
    }

    /// @notice Withdraw ERC20 profit to owner wallet
    function withdraw(address token) external onlyOwner {
        uint256 bal = IERC20(token).balanceOf(address(this));
        require(bal > 0, "Nothing to withdraw");
        IERC20(token).transfer(owner, bal);
    }

    /// @notice Withdraw any native MATIC accidentally sent
    function withdrawMATIC() external onlyOwner {
        payable(owner).transfer(address(this).balance);
    }

    receive() external payable {}
}
