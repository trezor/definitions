from .common import Network, Token

networks: list[Network] = [
    {
        "chain": "eth",
        "chain_id": 1,
        "coingecko_id": "ethereum",
        "is_testnet": False,
        "name": "Ethereum",
        "shortcut": "ETH",
        "slip44": 60,
    },
    {
        "chain": "exp",
        "chain_id": 2,
        "is_testnet": False,
        "name": "Expanse Network",
        "shortcut": "EXP",
        "slip44": 40,
    },
    {
        "chain": "rop",
        "chain_id": 3,
        "is_testnet": True,
        "name": "Ropsten",
        "shortcut": "tETH",
        "slip44": 1,
    },
    {
        "chain": "rin",
        "chain_id": 4,
        "is_testnet": True,
        "name": "Rinkeby",
        "shortcut": "tETH",
        "slip44": 1,
    },
]

tokens: list[Token] = [
    {
        "address": "0x00000000000045166c45af0fc6e4cf31d9e14b9a",
        "chain": "eth",
        "chain_id": 1,
        "coingecko_id": "topbidder",
        "decimals": 18,
        "name": "TopBidder",
        "shortcut": "BID",
    },
    {
        "address": "0x0000000000004946c0e9f43f4dee607b0ef1fa1c",
        "chain": "eth",
        "chain_id": 1,
        "coingecko_id": "chi-gastoken",
        "decimals": 0,
        "name": "Chi Gas",
        "shortcut": "CHI",
    },
    {
        "address": "0x000000000000d0151e748d25b766e77efe2a6c83",
        "chain": "eth",
        "chain_id": 1,
        "coingecko_id": "xdefi-governance-token",
        "decimals": 18,
        "name": "XDEFI Governance",
        "shortcut": "XDEX",
    },
    {
        "address": "0x0000000000085d4780b73119b644ae5ecd22b376",
        "chain": "eth",
        "chain_id": 1,
        "coingecko_id": "true-usd",
        "decimals": 18,
        "name": "TrueUSD",
        "shortcut": "TUSD",
    },
]
