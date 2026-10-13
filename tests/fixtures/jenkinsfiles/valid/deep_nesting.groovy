pipeline {
    agent { docker { image 'node:22' } }
    stages {
        stage('Level 1') {
            stages {
                stage('Level 2') {
                    stages {
                        stage('Level 3') {
                            steps { sh 'echo deep' }
                        }
                        stage('Fan out') {
                            parallel {
                                stage('A') { steps { sh 'echo a' } }
                                stage('B') {
                                    stages {
                                        stage('B1') { steps { sh 'echo b1' } }
                                        stage('B2') { steps { sh 'echo b2' } }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
}
