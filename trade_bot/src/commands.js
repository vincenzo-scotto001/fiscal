// Slash command definitions. scripts/register-commands.mjs sends these to Discord on deploy.
// Option types: 3 = text, 10 = number. The trade commands have default_member_permissions "0",
// which hides them from everyone except server admins (the worker also checks it's the owner).
// The Fiscal AI commands and /analysis at the end are for every member; /collar is the owner's.

const STYLE_CHOICES = [
  { name: "Day trade", value: "day" },
  { name: "Swing", value: "swing" },
  { name: "Medium-term", value: "medium" },
];

const ticker = { type: 3, name: "ticker", description: "Stock ticker, e.g. NVDA", required: true, max_length: 10 };
const position = {
  type: 3, name: "position", description: "Pick one of your open positions", required: true, autocomplete: true,
};
const exitPrice = {
  type: 10, name: "price",
  description: "Your price. Stocks: leave blank for the live price. Options: required",
  required: false, min_value: 0,
};

export const COMMANDS = [
  {
    name: "buy",
    description: "Post a stock buy to #trade, or to #invest for a long-term hold",
    default_member_permissions: "0",
    options: [
      ticker,
      {
        type: 3, name: "style", description: "Trade style", required: true,
        choices: [...STYLE_CHOICES, { name: "Long-term hold (#invest)", value: "long" }],
      },
      { type: 10, name: "price", description: "Your price. Leave blank for the live price", required: false, min_value: 0.0001 },
    ],
  },
  {
    name: "short",
    description: "Post a short sale to #trade",
    default_member_permissions: "0",
    options: [
      ticker,
      { type: 3, name: "style", description: "Trade style", required: true, choices: STYLE_CHOICES },
      { type: 10, name: "price", description: "Your price. Leave blank for the live price", required: false, min_value: 0.0001 },
    ],
  },
  {
    name: "option",
    description: "Post an options buy (call or put) to #trade",
    default_member_permissions: "0",
    options: [
      ticker,
      {
        type: 3, name: "type", description: "Call or put", required: true,
        choices: [{ name: "Call", value: "call" }, { name: "Put", value: "put" }],
      },
      { type: 10, name: "strike", description: "Strike price, e.g. 250", required: true, min_value: 0.01 },
      { type: 3, name: "expiry", description: "Expiry date, e.g. 12/4 or 2026-12-04", required: true, max_length: 20 },
      { type: 10, name: "price", description: "Price per share of the contract, e.g. 3.10", required: true, min_value: 0.01 },
      { type: 3, name: "style", description: "Trade style", required: true, choices: STYLE_CHOICES },
    ],
  },
  {
    name: "trim",
    description: "Post a partial sale in the position's thread (no ping)",
    default_member_permissions: "0",
    options: [position, exitPrice],
  },
  {
    name: "close",
    description: "Close a position and post the result",
    default_member_permissions: "0",
    options: [position, exitPrice],
  },
  {
    name: "positions",
    description: "Show your open positions (only you can see the reply)",
    default_member_permissions: "0",
  },
  {
    name: "ask-fiscal",
    description: "Ask Fiscal AI about markets, investing or the economy (3 a day, unlimited for Premium)",
    options: [{
      type: 3, name: "question", description: "Your question, e.g. What does an inverted yield curve mean?",
      required: true, min_length: 3, max_length: 500,
    }],
  },
  {
    name: "bull-bear",
    description: "Post 3 bull and 3 bear points for a stock (3 a day, unlimited for Premium)",
    options: [{ ...ticker }],
  },
  {
    name: "analysis",
    description: "Deep chart + options positioning analysis, posted in #analysis (3 a day, unlimited for Premium)",
    options: [{ ...ticker, description: "Ticker, e.g. NVDA, or an index: SPX, NDX, RUT" }],
  },
  {
    name: "collar",
    description: "Set the JPM collar strikes the SPX analysis uses (after each quarter-end roll)",
    default_member_permissions: "0",
    options: [
      { type: 10, name: "call", description: "Short call strike, e.g. 6020", required: false, min_value: 1 },
      { type: 10, name: "put", description: "Long put strike, e.g. 5245", required: false, min_value: 1 },
      { type: 10, name: "put_low", description: "Lower strike of the put spread, e.g. 4580", required: false, min_value: 1 },
      { type: 3, name: "expiry", description: "Expiry, e.g. Dec 31", required: false },
      { type: 5, name: "clear", description: "Remove the saved collar", required: false },
    ],
  },
];
