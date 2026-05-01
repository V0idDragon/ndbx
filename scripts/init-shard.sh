#!/bin/bash
set -e

echo "Waiting for config server and shards to be ready..."
sleep 15

mongosh --host mongo-config --port ${MONGODB_CONFIG_PORT} --eval "
rs.initiate({
  _id: 'cfg',
  configsvr: true,
  members: [{ _id: 0, host: 'mongo-config:${MONGODB_CONFIG_PORT}' }]
})
"
echo "Config server initialized."

mongosh --host mongo-shard1-1 --port ${MONGODB_SHARD_PORT} --eval "
rs.initiate({
  _id: 'shard1',
  members: [
    { _id: 0, host: 'mongo-shard1-1:${MONGODB_SHARD_PORT}' },
    { _id: 1, host: 'mongo-shard1-2:${MONGODB_SHARD_PORT}' },
    { _id: 2, host: 'mongo-shard1-3:${MONGODB_SHARD_PORT}' }
  ]
})
"
echo "Shard1 initialized."

# Init shard2
mongosh --host mongo-shard2-1 --port ${MONGODB_SHARD_PORT} --eval "
rs.initiate({
  _id: 'shard2',
  members: [
    { _id: 0, host: 'mongo-shard2-1:${MONGODB_SHARD_PORT}' },
    { _id: 1, host: 'mongo-shard2-2:${MONGODB_SHARD_PORT}' },
    { _id: 2, host: 'mongo-shard2-3:${MONGODB_SHARD_PORT}' }
  ]
})
"
echo "Shard2 initialized."

sleep 10

mongosh --host mongodb --port ${MONGODB_PORT} --eval "
sh.addShard('shard1/mongo-shard1-1:${MONGODB_SHARD_PORT},mongo-shard1-2:${MONGODB_SHARD_PORT},mongo-shard1-3:${MONGODB_SHARD_PORT}');
sh.addShard('shard2/mongo-shard2-1:${MONGODB_SHARD_PORT},mongo-shard2-2:${MONGODB_SHARD_PORT},mongo-shard2-3:${MONGODB_SHARD_PORT}');
"
echo "Shards added."

mongosh --host mongodb --port ${MONGODB_PORT} --eval '
sh.enableSharding("eventhub");
sh.shardCollection("eventhub.events", { "created_by": "hashed" });
'
echo "Sharding enabled for eventhub.events."
echo "MongoDB cluster initialization complete."