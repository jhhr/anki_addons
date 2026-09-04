// Debugging:
const log = false;
if (log) console.log(JSON.stringify(states, null, 4));
if (log) console.log(JSON.stringify(customData, null, 4));

// Custom Scheduler v1.0.0

const deckParams = [
  {
    // Default parameters of Custom Scheduler for global
    // Ensure that all keys are defined in this one
    deckName: "global config for Custom Scheduler",
    // Base interval that is multiplied by ease factor to get the max interval
    // at which the interval growth is at the minimum
    // For example, a card at 250% ease, that interval is 500 days when daysUpper is 200
    daysUpper: 200,
    // Minimum multiplier applied when answering again
    minAgainMult: 0
  },
  {
    // Example 1: Custom params used for one deck and all its sub-decks.
    // Not all keys need to be set. If a key is not set, the global config will be used.
    deckName: "MainDeck1",
    daysUpper: 250
  },
  {
    // Example 2a: Custom params for a parent deck and its subdecks
    deckName: "MainDeck2",
    daysUpper: 150
  },
  {
    // Example 2b: Custom params for a sub-deck of the parent deck
    // These will override the parent deck's parameters
    deckName: "MainDeck2::SubDeck::SubSubDeck",
    daysUpper: 100,
    minAgainMult: 0.25
  }
];

// To turn off Custom Scheduler in specific decks, fill them into the skip_decks list below.
// Please don't remove it even if you don't need it.
const skipDecks = [];

// Custom Scheduler supports displaying memory states of cards.
// Enable it for debugging if you encounter something wrong.
const displaySchedulerState = true;

// display if Custom Scheduler is enabled
if (displaySchedulerState) {
  let schedulerStatusStyle = document.getElementById("scheduler_status_style");
  if (!schedulerStatusStyle) {
    schedulerStatusStyle = document.createElement("style");
    schedulerStatusStyle.id = "scheduler_status_style";
    schedulerStatusStyle.innerHTML = `
      #scheduler_status_container {
        position: absolute;
        top: 0;
        right: 0;
        font-size: 0.9em;
        opacity: 0.3;
        font-family: monospace;
        z-index: 1000;
        cursor: pointer;
        pointer-events: all;
      }
      #scheduler_status_trigger {
        font-size: 0.9em;
        padding: 0.2em 0.6em;
        margin: 0;
        border-radius: 0.3em;
        border: 1px solid transparent;
        cursor: pointer;
        z-index: 1000;
        float: right;
        pointer-events: all;
      }
      #scheduler_status_trigger:hover {
        border-color: gray;
      }
      #scheduler_status {
        position: relative;
        top: 1.8em;
        right: 0;
        display: none;
        padding: 0.5em;
        font-family: monospace;
        text-align: left;
        line-height: 1em;
        border-radius: 0.3em;
        border: 1px solid darkgray;
        background-color: #303030;
        color: white;
      }
      #scheduler_status_container.active #scheduler_status {
        display: block;
      }
      #scheduler_status_container.inactive #scheduler_status {
        display: none;
      }
      #scheduler_status_container.active #scheduler_status_trigger:after {
        content: "🛠️ ▲";
      }
      #scheduler_status_container.inactive #scheduler_status_trigger:after {
        content: "🛠️ ▼";
      }
      #scheduler_status_container.active {
        opacity: 0.9;
      }
      #scheduler_status_container.inactive {
        opacity: 0.5;
      }
    `;
    document.head.appendChild(schedulerStatusStyle);
  }
  let schedulerStatusContainer = document.getElementById(
    "scheduler_status_container"
  );
  if (!schedulerStatusContainer) {
    schedulerStatusContainer = document.createElement("div");
    const trigger = document.createElement("button");
    trigger.id = "scheduler_status_trigger";
    schedulerStatusContainer.appendChild(trigger);
    schedulerStatusContainer.id = "scheduler_status_container";
    // position the trigger to the top right corner of the window
    schedulerStatusContainer.className = "inactive";
    // In order to allow transporting the onlick function with outerHTML into storage as a string,
    // the onclick function is defined as a string and will be evaluated when the element is created.
    // Note the body.onclick = hide; at the end
    // This is hack to enable closing the popover by clicking outside of it in such a way that
    // it too is conveniently transported along with the element.
    // event.stopPropagation(); is used to prevent the click event from bubbling up to the body
    // and triggering the hide function immediately after the show function is called.
    schedulerStatusContainer.setAttribute(
      "onclick",
      `
      event.stopPropagation();
      (function() {
        function hide() {
          document.getElementById("scheduler_status_container").className = "inactive";
        };
        function show() {
          document.getElementById("scheduler_status_container").className = "active";
        }
        switch (document.getElementById("scheduler_status_container").className) {
          case "inactive":
            show();
            break;
          case "active":
            hide();
            break;
        }
        document.body.onclick = hide;
      })();
    `
    );
    document.body.appendChild(schedulerStatusContainer);
  }
  // Replace the previous status element, if it exists
  const prevStatus = document.getElementById("scheduler_status");
  if (prevStatus) {
    prevStatus.remove();
  }
  // Create an element to display the scheduler status that will be shown as a popover
  var schedulerStatus = document.createElement("div");
  schedulerStatus.innerHTML = "<br>Custom scheduler enabled";
  schedulerStatus.id = "scheduler_status";
  schedulerStatusContainer.appendChild(schedulerStatus);
}

const globalDeckParams = deckParams.find(
  (deck) => deck.deckName === "global config for Custom Scheduler"
);
if (!globalDeckParams) {
  if (displaySchedulerState) {
    schedulerStatus.innerHTML +=
      '<br><span style="color:red;">ERROR: Global config not found</span>';
  }
}
let currentDeckParams = globalDeckParams;

let deckName = getDeckname();
if (!deckName) {
  if (displaySchedulerState) {
    schedulerStatus.innerHTML +=
      '<br><span style="color:red;">ERROR: <div id="deck" deckName="..."> not found. Global config will be used.</span>';
  }
} else {
  if (skipDecks.some((skipDeck) => deckName.startsWith(skipDeck))) {
    if (displaySchedulerState) {
      schedulerStatus.innerHTML +=
        "<br>Custom scheduler disabled for this deck";
    }
    return;
  }
  // Arrange the deckParams of parent decks in front of their sub decks.
  // This is so that we can define parameters for a parent deck and have them apply to all
  // sub-decks while still being able to override them for specific sub-decks without
  // having to define the same parameters for each sub-deck.
  deckParams.sort(function (a, b) {
    return a.deckName.localeCompare(b.deckName);
  });
  for (let i = 0; i < deckParams.length; i++) {
    if (deckName.startsWith(deckParams[i]["deckName"])) {
      foundParams = true;
      currentDeckParams = {
        ...currentDeckParams,
        ...deckParams[i]
      };
      // continue looping and overwriting the parameters with the next matching sub-deck's
      // parameters, if there are any
    }
  }
}
if (displaySchedulerState) {
  // The last matched deck parameters name will the deckName, parameters will be a combination
  // of global parameters and all parent deck parameters
  schedulerStatus.innerHTML += `<br><strong>Deck parameters:</strong>
<ul">
${Object.entries(currentDeckParams)
  .map(([key, value]) => `<li>${key}: ${value}</li>`)
  .join("")}
</ul>`;
}

const currentCustomData = JSON.parse(states.current.customData);
storeData();

// Set the customData values to 0 for all buttons so that rescheduling and ease adjustment will be applied
customData.again.v = 0;
customData.hard.v = 0;
customData.good.v = 0;
customData.easy.v = 0;

customData.again.e = 0;
customData.hard.e = 0;
customData.good.e = 0;
customData.easy.e = 0;

customData.again.fc = 0;
customData.hard.fc = 0;
customData.good.fc = 0;
customData.easy.fc = 0;

// Don't adjust intervals for new or new cards still in learning steps
if (
  states.current.normal?.new ||
  states.current.normal?.learning ||
  states.current.filtered?.rescheduling.originalState.new ||
  states.current.filtered?.rescheduling.originalState.learning
)
  return;

// Do adjust for reviews or cards in relearning steps
const revObj =
  states.current.normal?.review ||
  states.current.normal?.relearning?.review ||
  states.current.filtered?.rescheduling?.originalState?.review ||
  states.current.filtered?.rescheduling?.originalState?.relearning.review;

const curFct = revObj?.easeFactor;
const curRevIvl = revObj?.scheduledDays;

const { daysUpper: daysUpper, minAgainMult = 0 } = currentDeckParams;
const minModFct = Math.sqrt(curFct);
const adjDaysUpper = daysUpper * curFct;

const againRevObj =
  states.again.normal?.review ||
  states.again.normal?.relearning?.review ||
  states.again.filtered?.rescheduling?.originalState?.review ||
  states.again.filtered?.rescheduling?.originalState?.relearning.review ||
  {};
const { scheduledDays: againIvl } = againRevObj;
const { scheduledDays: hardIvl } = states.hard.normal?.review || {};
const { scheduledDays: goodIvl } = states.good.normal?.review || {};
const { scheduledDays: easyIvl } = states.easy.normal?.review || {};
const againIvlMult = Number.isFinite(againIvl) && againIvl / curRevIvl;
const hardIvlMult = Number.isFinite(hardIvl) && hardIvl / curRevIvl;
const easyIvlMult =
  Number.isFinite(easyIvl) && Number.isFinite(goodIvl) && easyIvl / goodIvl;

if (log) console.log("adjDaysUpper", adjDaysUpper);
if (log) console.log("curFct", curFct);
if (log) console.log("minModFct", minModFct);
if (log) console.log("curRevIvl", curRevIvl);
if (log) console.log("againIvlMult", againIvlMult);
if (log) console.log("hardIvlMult", hardIvlMult);
if (log) console.log("easyIvlMult", easyIvlMult);

function getDeckname() {
  return (
    ctx?.deckName || document.getElementById("deck")?.getAttribute("deckName")
  );
}

function isReview() {
  if (states.current.normal?.review !== undefined) {
    if (states.current.normal?.review !== null) {
      return true;
    }
  }
  if (states.current.filtered?.rescheduling?.originalState !== undefined) {
    if (
      Object.hasOwn(
        states.current.filtered?.rescheduling?.originalState,
        "review"
      )
    ) {
      return true;
    }
  }
  return false;
}

// Global fuzz factor for all ratings.
const fuzzFactor = setFuzzFactor();
console.log("fuzzFactor", fuzzFactor);

function applyFuzz(ivl) {
  if (ivl < 2.5) return ivl;
  ivl = Math.round(ivl);
  let min_ivl = Math.max(2, Math.round(ivl * 0.95 - 1));
  let max_ivl = Math.round(ivl * 1.05 + 1);
  if (isReview()) {
    if (ivl > curRevIvl) {
      min_ivl = Math.max(min_ivl, curRevIvl + 1);
    }
  }
  return Math.floor(fuzzFactor * (max_ivl - min_ivl + 1) + min_ivl);
}

function adjustIvl(answerIvl, mult) {
  const ratio = Math.min(answerIvl / adjDaysUpper, 1);
  const minModMult = Math.sqrt(mult);
  const modMult = Math.min(mult, mult * (1 - ratio) + minModMult * ratio);
  const modIvl = Math.min(answerIvl, curRevIvl * modMult);
  if (log) console.log("ratio", ratio);
  if (log) console.log("minModMult", minModMult);
  if (log) console.log("modMult", modMult);
  if (log) console.log("modIvl", modIvl);

  return Math.ceil(applyFuzz(modIvl));
}

if (curRevIvl) {
  if (againIvlMult) {
    let { sr: successRate = 0.99 } = currentCustomData;
    successRate = parseFloat(successRate);
    // Use successRate to adjust the again multiplier from default
    // The lower the sucessRate, the more answering again reduces ivl
    const modAgainMult = Math.max(
      againIvlMult - (1 - successRate),
      minAgainMult
    );
    const modAgainIvl = Math.ceil(curRevIvl * modAgainMult);
    if (log) {
      console.log("mod again", againIvl, modAgainIvl);
      console.log("modAgainMult", modAgainMult);
      console.log("minAgainMult", minAgainMult);
      console.log("successRate", successRate);
    }
    if (againRevObj) {
      againRevObj.scheduledDays = modAgainIvl;
    }
  }

  if (hardIvl && hardIvlMult && hardIvlMult >= 1 && goodIvl) {
    const hardGoodRatio = Math.min(hardIvl / goodIvl, 1);
    // Try to keep a significant difference between hard and good ivls even when factor may be low
    // Return a ivl between the unchanged hardIvl and curIvl that's closes to curIvl the closer
    // hardIvl is to goodIvl
    const modHardIvl = Math.ceil(
      hardIvl * (1 - hardGoodRatio) + curRevIvl * hardGoodRatio
    );
    if (log) console.log("hardGoodRatio", hardGoodRatio);
    if (log) console.log("mod hard", hardIvl, modHardIvl);
    if (states.hard.normal?.review) {
      states.hard.normal.review.scheduledDays = modHardIvl;
    }
  }

  let goodModIvl;
  if (goodIvl) {
    goodModIvl = adjustIvl(goodIvl, curFct);
    if (log) console.log("mod good", goodIvl, goodModIvl);
    if (states.good.normal?.review) {
      states.good.normal.review.scheduledDays = goodModIvl;
    }
  }

  if (goodModIvl && easyIvlMult) {
    const modEasyIvl = Math.ceil(goodModIvl * easyIvlMult);
    if (log) console.log("mod easy", easyIvl, modEasyIvl);
    if (states.easy.normal?.review) {
      states.easy.normal.review.scheduledDays = modEasyIvl;
    }
  }
}

function get_seed() {
  if (
    !customData.again.s |
    !customData.hard.s |
    !customData.good.s |
    !customData.easy.s
  ) {
    if (typeof ctx !== "undefined" && ctx.seed) {
      return ctx.seed;
    } else {
      return document.getElementById("qa").innerText;
    }
  } else {
    return customData.good.s;
  }
}
function setFuzzFactor() {
  // Note: Originally copied from seedrandom.js package (https://github.com/davidbau/seedrandom)
  !(function (f, a, c) {
    var s,
      l = 256,
      p = "random",
      d = c.pow(l, 6),
      g = c.pow(2, 52),
      y = 2 * g,
      h = l - 1;
    function n(n, t, r) {
      function e() {
        for (var n = u.g(6), t = d, r = 0; n < g; )
          (n = (n + r) * l), (t *= l), (r = u.g(1));
        for (; y <= n; ) (n /= 2), (t /= 2), (r >>>= 1);
        return (n + r) / t;
      }
      var o = [],
        i = j(
          (function n(t, r) {
            var e,
              o = [],
              i = typeof t;
            if (r && "object" == i)
              for (e in t)
                try {
                  o.push(n(t[e], r - 1));
                } catch (n) {}
            return o.length ? o : "string" == i ? t : t + "\0";
          })(
            (t = 1 == t ? { entropy: !0 } : t || {}).entropy
              ? [n, S(a)]
              : null == n
              ? (function () {
                  try {
                    var n;
                    return (
                      s && (n = s.randomBytes)
                        ? (n = n(l))
                        : ((n = new Uint8Array(l)),
                          (f.crypto || f.msCrypto).getRandomValues(n)),
                      S(n)
                    );
                  } catch (n) {
                    var t = f.navigator,
                      r = t && t.plugins;
                    return [+new Date(), f, r, f.screen, S(a)];
                  }
                })()
              : n,
            3
          ),
          o
        ),
        u = new m(o);
      return (
        (e.int32 = function () {
          return 0 | u.g(4);
        }),
        (e.quick = function () {
          return u.g(4) / 4294967296;
        }),
        (e.double = e),
        j(S(u.S), a),
        (
          t.pass ||
          r ||
          function (n, t, r, e) {
            return (
              e &&
                (e.S && v(e, u),
                (n.state = function () {
                  return v(u, {});
                })),
              r ? ((c[p] = n), t) : n
            );
          }
        )(e, i, "global" in t ? t.global : this == c, t.state)
      );
    }
    function m(n) {
      var t,
        r = n.length,
        u = this,
        e = 0,
        o = (u.i = u.j = 0),
        i = (u.S = []);
      for (r || (n = [r++]); e < l; ) i[e] = e++;
      for (e = 0; e < l; e++)
        (i[e] = i[(o = h & (o + n[e % r] + (t = i[e])))]), (i[o] = t);
      (u.g = function (n) {
        for (var t, r = 0, e = u.i, o = u.j, i = u.S; n--; )
          (t = i[(e = h & (e + 1))]),
            (r = r * l + i[h & ((i[e] = i[(o = h & (o + t))]) + (i[o] = t))]);
        return (u.i = e), (u.j = o), r;
      })(l);
    }
    function v(n, t) {
      return (t.i = n.i), (t.j = n.j), (t.S = n.S.slice()), t;
    }
    function j(n, t) {
      for (var r, e = n + "", o = 0; o < e.length; )
        t[h & o] = h & ((r ^= 19 * t[h & o]) + e.charCodeAt(o++));
      return S(t);
    }
    function S(n) {
      return String.fromCharCode.apply(0, n);
    }
    if ((j(c.random(), a), "object" == typeof module && module.exports)) {
      module.exports = n;
      try {
        s = require("crypto");
      } catch (n) {}
    } else
      "function" == typeof define && define.amd
        ? define(function () {
            return n;
          })
        : (c["seed" + p] = n);
  })("undefined" != typeof self ? self : this, [], Math);
  // MIT License
  // Copyright 2019 David Bau.
  // Permission is hereby granted, free of charge, to any person obtaining a copy
  // of this software and associated documentation files (the "Software"), to deal
  // in the Software without restriction, including without limitation the rights
  // to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
  // copies of the Software, and to permit persons to whom the Software is
  // furnished to do so, subject to the following conditions:
  // The above copyright notice and this permission notice shall be included in all
  // copies or substantial portions of the Software.
  // THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
  // IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
  // FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
  // AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
  // LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
  // OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
  // SOFTWARE.
  let seed = get_seed();
  const generator = new Math.seedrandom(seed);
  const fuzz_factor = generator();
  seed = Math.round(fuzz_factor * 10000);
  customData.again.s = (seed + 1) % 10000;
  customData.hard.s = (seed + 2) % 10000;
  customData.good.s = (seed + 3) % 10000;
  customData.easy.s = (seed + 4) % 10000;
  return fuzz_factor;
}

if (log) console.log(JSON.stringify(states, null, 4));
if (log) console.log(JSON.stringify(customData, null, 4));

// Store data we want to show in the card back
// or just access in either front or back
function storeData() {
  // v1.1.8 - https://github.com/SimonLammer/anki-persistence/blob/584396fea9dea0921011671a47a0fdda19265e62/script.js
  if (void 0 === window.Persistence) {
    var e = "github.com/SimonLammer/anki-persistence/",
      t = "_default";
    if (
      ((window.Persistence_sessionStorage = function () {
        var i = !1;
        try {
          "object" == typeof window.sessionStorage &&
            ((i = !0),
            (this.clear = function () {
              for (var t = 0; t < sessionStorage.length; t++) {
                var i = sessionStorage.key(t);
                0 == i.indexOf(e) && (sessionStorage.removeItem(i), t--);
              }
            }),
            (this.setItem = function (i, n) {
              void 0 == n && ((n = i), (i = t)),
                sessionStorage.setItem(e + i, JSON.stringify(n));
            }),
            (this.getItem = function (i) {
              return (
                void 0 == i && (i = t),
                JSON.parse(sessionStorage.getItem(e + i))
              );
            }),
            (this.removeItem = function (i) {
              void 0 == i && (i = t), sessionStorage.removeItem(e + i);
            }),
            (this.getAllKeys = function () {
              for (
                var t = [], i = Object.keys(sessionStorage), n = 0;
                n < i.length;
                n++
              ) {
                var s = i[n];
                0 == s.indexOf(e) && t.push(s.substring(e.length, s.length));
              }
              return t.sort();
            }));
        } catch (n) {}
        this.isAvailable = function () {
          return i;
        };
      }),
      (window.Persistence_windowKey = function (i) {
        var n = window[i],
          s = !1;
        "object" == typeof n &&
          ((s = !0),
          (this.clear = function () {
            n[e] = {};
          }),
          (this.setItem = function (i, s) {
            void 0 == s && ((s = i), (i = t)), (n[e][i] = s);
          }),
          (this.getItem = function (i) {
            return void 0 == i && (i = t), void 0 == n[e][i] ? null : n[e][i];
          }),
          (this.removeItem = function (i) {
            void 0 == i && (i = t), delete n[e][i];
          }),
          (this.getAllKeys = function () {
            return Object.keys(n[e]);
          }),
          void 0 == n[e] && this.clear()),
          (this.isAvailable = function () {
            return s;
          });
      }),
      (window.Persistence = new Persistence_sessionStorage()),
      Persistence.isAvailable() ||
        (window.Persistence = new Persistence_windowKey("py")),
      !Persistence.isAvailable())
    ) {
      var i = window.location.toString().indexOf("title"),
        n = window.location.toString().indexOf("main", i);
      i > 0 &&
        n > 0 &&
        n - i < 10 &&
        (window.Persistence = new Persistence_windowKey("qt"));
    }
  }

  if (Persistence.isAvailable()) {
    Persistence.setItem("customData", currentCustomData);
    Persistence.setItem(
      "schedulerStatusHTML",
      document.getElementById("scheduler_status_container")?.outerHTML
    );
    Persistence.setItem(
      "schedulerStatusStyle",
      document.getElementById("scheduler_status_style")?.outerHTML
    );
    // Fire a custom event to notify that template code can listen to and then try
    // to get the data (Only works on AnkiDroid)
    window.dispatchEvent(new Event("SchedulerDataStored"));
  }
}
