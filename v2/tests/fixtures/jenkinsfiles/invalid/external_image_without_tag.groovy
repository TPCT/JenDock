// expect: line 3: tag or digest
pipeline {
    agent { docker { image 'node' } }
    stages { stage('A') { steps { sh 'node -v' } } }
}
